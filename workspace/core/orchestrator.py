# workspace/core/orchestrator.py
import json
import re
from typing import AsyncGenerator, Dict, List

import workspace.config as config
from workspace.core.database import Database
from workspace.core.model import ModelService, PlanStep
from workspace.tools.code_tools import ProjectWorkspace
from workspace.tools.doc_search import DocSearchEngine


class TaskOrchestrator:
    def __init__(
        self,
        model_service: ModelService,
        db: Database,
        workspace: ProjectWorkspace,
        doc_search: DocSearchEngine,
    ):
        self.model = model_service
        self.db = db
        self.workspace = workspace
        self.doc_search = doc_search

    @staticmethod
    def _extract_python_code(text: str) -> str:
        if "```python" in text:
            return text.split("```python")[1].split("```")[0].strip()
        if "```" in text:
            return text.split("```")[1].split("```")[0].strip()
        return text.strip()

    @staticmethod
    def _extract_patch_json(text: str) -> tuple[str, str] | None:
        cleaned = text.strip()
        if "```" in cleaned:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
            if match:
                cleaned = match.group(1)
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict) and "old_code" in data and "new_code" in data:
                return data["old_code"], data["new_code"]
        except Exception:
            pass
        return None

    async def execute_task(
        self,
        project_id: int,
        chat_id: int,
        user_prompt: str,
    ) -> AsyncGenerator[str, None]:
        """Полный цикл: Память -> AST-карта -> Structured Plan -> Исполнение с валидацией и откатом."""
        output_log: List[str] = []

        def emit(chunk: str) -> str:
            output_log.append(chunk)
            return chunk

        # 1. Сохраняем запрос пользователя в SQLite
        if self.db.count_chat_messages(chat_id) == 0:
            self.db.update_chat_title(chat_id, user_prompt)
        self.db.add_message(chat_id, "user", user_prompt)

        # 2. Загружаем сжатый паспорт проекта и скользящее окно истории
        rolling_summary = self.db.get_project_summary(project_id)
        recent_history = self.db.get_recent_messages(chat_id, limit=config.MAX_HISTORY_MESSAGES)[:-1]

        yield emit(f"🔍 *Анализирую AST-структуру проекта ({self.model.get_model_id()})...*\n\n")
        repo_map = self.workspace.generate_repo_map()

        # 3. Планирование по строгой JSON-схеме
        plan = await self.model.generate_plan(
            prompt=user_prompt,
            repo_map=repo_map,
            rolling_summary=rolling_summary,
            recent_history=recent_history,
        )

        # Нормализуем шаги плана перед отображением и запуском
        steps = self._normalize_plan(plan.steps)

        # Если задача сводится к прямому ответу без работы с файлами — стримим ответ напрямую
        if len(steps) == 1 and steps[0].action == "answer":
            async for token in self.model.generate_answer(
                user_prompt=user_prompt,
                docs_context="",
                recent_history=recent_history,
            ):
                yield emit(token)
            self._finalize_session(project_id, chat_id, "".join(output_log))
            return

        # Выводим чеклист шагов
        yield emit("📋 **План выполнения:**\n")
        for idx, step in enumerate(steps, 1):
            target_label = f"`{step.file}` — " if step.file else ""
            yield emit(f"{idx}. **{step.action.upper()}**: {target_label}{step.instruction}\n")
        yield emit("\n---\n")

        # Буфер состояния текущей сессии
        loaded_files: Dict[str, str] = {}
        snapshots: Dict[str, str] = {}
        docs_context: List[str] = []
        modified_files: List[str] = []

        # 4. Детерминированное исполнение шагов
        for idx, step in enumerate(plan.steps, 1):
            action = step.action

            if action == "read":
                yield emit(f"\n> 📖 *Шаг {idx}/{len(plan.steps)}: Читаю* `{step.file}`...\n")
                content = self.workspace.read_source_file(step.file)
                loaded_files[step.file] = content

            elif action == "search_docs":
                yield emit(f"\n> 🔎 *Шаг {idx}/{len(plan.steps)}: Ищу в документации:* «{step.instruction}»...\n")
                found = self.doc_search.search_docs(step.instruction)
                docs_context.append(found)
                yield emit(f"> 📘 *Результаты поиска добавлены в контекст.*\n")

            elif action in ("patch", "modify", "create"):
                target_on_disk = self.workspace.resolve_safe_path(step.file)
                if action == "create" and target_on_disk and target_on_disk.is_file():
                    action = "modify"
                    step.action = "modify"

                yield emit(f"\n> 🧠 *Шаг {idx}/{len(plan.steps)}: {action.upper()} для* `{step.file}`...\n")

                for ctx_file in step.context_files:
                    if ctx_file and ctx_file not in loaded_files:
                        loaded_files[ctx_file] = self.workspace.read_source_file(ctx_file)

                if action in ("patch", "modify") and step.file not in loaded_files:
                    loaded_files[step.file] = self.workspace.read_source_file(step.file)

                if step.file in loaded_files and step.file not in snapshots:
                    snapshots[step.file] = loaded_files[step.file]

                success, status_msg = await self._execute_code_step_with_validation(
                    step=step,
                    user_prompt=user_prompt,
                    loaded_files=loaded_files,
                    docs_context=docs_context,
                )
                if success:
                    if step.file not in modified_files:
                        modified_files.append(step.file)
                    yield emit(f"> ✅ *{status_msg}*\n")
                else:
                    yield emit(f"> ⚠️ *{status_msg}*\n")

            elif action == "verify":
                cmd = step.instruction or "python3 run_test.py"
                yield emit(f"\n> 🧪 *Шаг {idx}/{len(steps)}: Проверка* `{cmd}`...\n")
                ok, test_out = self.workspace.run_verification_command(cmd)

                if ok:
                    yield emit(f"> ✅ *Проверка пройдена:*\n```text\n{test_out}\n```\n")
                else:
                    yield emit(
                        f"> ❌ *Ошибка при проверке:*\n```text\n{test_out}\n```\n"
                        f"> 🔄 *Запускаю автоисправление...*\n"
                    )
                    planned_edit_files = [
                        s.file for s in steps if s.action in ("patch", "modify", "create") and s.file
                    ]
                    fixed = await self._backtrack_and_fix(
                        modified_files=modified_files or planned_edit_files,
                        user_prompt=user_prompt,
                        error_log=test_out,
                        verify_cmd=cmd,
                        loaded_files=loaded_files,
                        snapshots=snapshots,
                        docs_context=docs_context,
                    )
                    if fixed:
                        yield emit(f"> ✅ *Код исправлен, повторная проверка пройдена!*\n")
                    else:
                        yield emit(f"> ⚠️ *Автоисправление не устранило ошибку. Откат файла выполнен.*\n")

            elif action == "answer":
                yield emit("\n> ✍️ *Формирую ответ...*\n\n")
                async for token in self.model.generate_answer(
                    user_prompt=user_prompt,
                    docs_context="\n\n".join(docs_context),
                    recent_history=recent_history,
                ):
                    yield emit(token)
                yield emit("\n")
                
        yield emit("\n\n🏁 **Выполнение плана завершено.**")
        self._finalize_session(project_id, chat_id, "".join(output_log))

    async def _execute_code_step_with_validation(
        self,
        step: PlanStep,
        user_prompt: str,
        loaded_files: Dict[str, str],
        docs_context: List[str],
    ) -> tuple[bool, str]:
        """Генерирует патч по JSON-схеме с фоллбэком на полную генерацию и проверкой AST."""
        context_str = self._build_context_string(loaded_files, docs_context, exclude_file=step.file)
        current_code = loaded_files.get(step.file, "")
        error_feedback = ""

        for attempt in range(config.MAX_FIX_RETRIES + 1):
            # На последней попытке для небольших файлов переключаемся с patch на полную генерацию
            use_patch = (
                step.action == "patch"
                and current_code
                and not current_code.startswith("Ошибка:")
                and attempt < config.MAX_FIX_RETRIES
            )

            if use_patch:
                user_msg = (
                    f"{context_str}\n"
                    f"ЦЕЛЕВОЙ ФАЙЛ `{step.file}`:\n```python\n{current_code}\n```\n\n"
                    f"ИСХОДНЫЙ ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n{user_prompt}\n\n"
                    f"ЗАДАЧА ТЕКУЩЕГО ШАГА: {step.instruction}\n"
                    f"{error_feedback}"
                )
                patch = await self.model.generate_patch(config.CODER_PATCH_PROMPT, user_msg)
                if not patch:
                    error_feedback = "\nОШИБКА: Не удалось сформировать патч. Укажи точный блок old_code из файла."
                    continue

                applied, candidate_code = self.workspace.apply_patch_in_memory(
                    current_code, patch.old_code, patch.new_code
                )
                if not applied:
                    error_feedback = f"\nОШИБКА: {candidate_code}"
                    continue
            else:
                user_msg = (
                    f"{context_str}\n"
                    f"ИСХОДНЫЙ КОД `{step.file}`:\n```python\n{current_code}\n```\n\n"
                    f"ИСХОДНЫЙ ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n{user_prompt}\n\n"
                    f"ЗАДАЧА ТЕКУЩЕГО ШАГА: {step.instruction}\n"
                    f"{error_feedback}"
                )
                raw = await self.model.generate_code(config.CODER_FULL_PROMPT, user_msg)
                candidate_code = self._extract_python_code(raw)

            if step.file.endswith(".py"):
                syntax_ok, syntax_msg = self.workspace.validate_python_syntax(candidate_code, step.file)
                if not syntax_ok:
                    error_feedback = f"\nСИНТАКСИЧЕСКАЯ ОШИБКА В ТВОЕМ КОДЕ: {syntax_msg}. Исправь её."
                    continue

            saved, save_msg = self.workspace.write_source_file(step.file, candidate_code)
            if saved:
                loaded_files[step.file] = candidate_code
            return saved, save_msg

        return False, f"Не удалось сгенерировать корректный код для `{step.file}`."
    
    async def _backtrack_and_fix(
        self,
        modified_files: List[str],
        user_prompt: str,
        error_log: str,
        verify_cmd: str,
        loaded_files: Dict[str, str],
        snapshots: Dict[str, str],
        docs_context: List[str],
    ) -> bool:
        """Изолированно исправляет каждый затронутый файл по логу ошибки или откатывает снимки."""
        candidates = [f for f in reversed(modified_files) if f in loaded_files]
        if not candidates:
            return False

        for c in candidates:
            if c not in snapshots:
                snapshots[c] = loaded_files[c]

        # Подгружаем код тестового скрипта (например, run_test.py) в контекст исправления
        for token in verify_cmd.split():
            if token.endswith(".py") and token not in loaded_files:
                test_src = self.workspace.read_source_file(token)
                if not test_src.startswith("Ошибка:"):
                    loaded_files[token] = test_src

        for target_file in candidates:
            # Возвращаем остальные файлы к чистым снимкам, чтобы их случайные поломки не мешали проверке
            for other_file, snap_code in snapshots.items():
                if other_file != target_file:
                    self.workspace.write_source_file(other_file, snap_code)
                    loaded_files[other_file] = snap_code

            context_str = self._build_context_string(loaded_files, docs_context, exclude_file=target_file)
            for _ in range(config.MAX_FIX_RETRIES):
                broken_code = loaded_files[target_file]
                fix_prompt = (
                    f"{context_str}\n"
                    f"ТЕКУЩИЙ КОД `{target_file}`, НЕ ПРОШЕДШИЙ ПРОВЕРКУ:\n```python\n{broken_code}\n```\n\n"
                    f"ИСХОДНЫЙ ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n{user_prompt}\n\n"
                    f"ЛОГ ОШИБКИ ВЫПОЛНЕНИЯ (`{verify_cmd}`):\n```\n{error_log}\n```\n\n"
                    f"Исправь причину ошибки и выведи ПОЛНЫЙ рабочий код файла `{target_file}`."
                )
                raw = await self.model.generate_code(config.CODER_FULL_PROMPT, fix_prompt)
                fixed_code = self._extract_python_code(raw)

                if target_file.endswith(".py"):
                    syntax_ok, _ = self.workspace.validate_python_syntax(fixed_code, target_file)
                    if not syntax_ok:
                        continue

                self.workspace.write_source_file(target_file, fixed_code)
                loaded_files[target_file] = fixed_code

                ok, new_out = self.workspace.run_verification_command(verify_cmd)
                if ok:
                    return True
                error_log = new_out

        # Восстанавливаем все исходные снимки при неудаче
        for snap_file, snap_code in snapshots.items():
            self.workspace.write_source_file(snap_file, snap_code)
            loaded_files[snap_file] = snap_code
        return False

    @staticmethod
    def _build_context_string(
        loaded_files: Dict[str, str],
        docs_context: List[str],
        exclude_file: str = "",
    ) -> str:
        parts = []
        if docs_context:
            parts.append("ДОКУМЕНТАЦИЯ:\n" + "\n\n".join(docs_context))
        for path, code in loaded_files.items():
            if path != exclude_file and not code.startswith("Ошибка:"):
                parts.append(f"СВЯЗАННЫЙ ФАЙЛ `{path}`:\n```python\n{code}\n```")
        return "\n\n".join(parts)

    def _finalize_session(self, project_id: int, chat_id: int, assistant_output: str) -> None:
        self.db.add_message(chat_id, "assistant", assistant_output)

    def _normalize_plan(self, steps: List[PlanStep]) -> List[PlanStep]:
        """Объединяет несколько правок одного файла в один шаг и защищает существующие файлы от create."""
        normalized: List[PlanStep] = []
        edit_steps_by_file: Dict[str, PlanStep] = {}

        for step in steps:
            if step.action == "create" and step.file:
                target = self.workspace.resolve_safe_path(step.file)
                if target and target.is_file():
                    step.action = "modify"

            if step.action in ("patch", "modify") and step.file:
                if step.file in edit_steps_by_file:
                    existing = edit_steps_by_file[step.file]
                    existing.action = "modify"
                    existing.instruction = f"{existing.instruction}\nДополнительно: {step.instruction}"
                    for cf in step.context_files:
                        if cf not in existing.context_files:
                            existing.context_files.append(cf)
                else:
                    edit_steps_by_file[step.file] = step
                    normalized.append(step)
            else:
                normalized.append(step)

        return normalized
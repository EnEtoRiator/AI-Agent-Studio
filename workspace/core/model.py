import json
import re
from typing import List, Tuple, Literal, AsyncGenerator
from pydantic import BaseModel, Field
from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

import workspace.config as config

class CodePatch(BaseModel):
    old_code: str = Field(description="Точный существующий фрагмент кода из файла с сохранением всех отступов")
    new_code: str = Field(description="Новый фрагмент кода для замены")

class PlanStep(BaseModel):
    action: Literal["read", "search_docs", "patch", "modify", "create", "verify", "answer"] = Field(
        description="Тип действия шага"
    )
    file: str = Field(
        default="",
        description="Относительный путь к целевому файлу (пусто для search_docs, verify, answer)",
    )
    context_files: List[str] = Field(
        default_factory=list,
        description="Список связанных файлов, интерфейсы которых нужны для этого шага",
    )
    instruction: str = Field(
        default="",
        description="Детальная инструкция для кодера, поисковый запрос, команда проверки или текст ответа",
    )


class ExecutionPlan(BaseModel):
    steps: List[PlanStep] = Field(description="Последовательный список шагов выполнения задачи")


class ModelService:
    def __init__(self, profile_key: str = config.DEFAULT_PROFILE):
        self.profile_key = profile_key if profile_key in config.MODEL_PROFILES else config.DEFAULT_PROFILE

    def set_profile(self, profile_key: str) -> None:
        if profile_key in config.MODEL_PROFILES:
            self.profile_key = profile_key

    def get_model_id(self) -> str:
        return config.MODEL_PROFILES[self.profile_key]["id"]

    def _build_llm(self, num_ctx: int) -> ChatOllama:
        return ChatOllama(
            model=self.get_model_id(),
            temperature=config.TEMPERATURE,
            num_ctx=num_ctx,
            keep_alive=config.KEEP_ALIVE,
            base_url=config.OLLAMA_BASE_URL,
        )

    async def generate_plan(
        self,
        prompt: str,
        repo_map: str,
        rolling_summary: str,
        recent_history: List[Tuple[str, str]],
    ) -> ExecutionPlan:
        """Генерирует план по строгой JSON-схеме с пониженным бюджетом контекста."""
        llm = self._build_llm(num_ctx=config.PLANNER_NUM_CTX)

        summary_block = f"ПАСПОРТ ПРОЕКТА:\n{rolling_summary}\n\n" if rolling_summary else ""
        messages = [SystemMessage(content=config.PLANNER_PROMPT)]

        for role, text in recent_history:
            if role == "user":
                messages.append(HumanMessage(content=text))
            elif role == "assistant":
                # Не передаем технические логи прошлых планов, чтобы 3B не копировала их шаблон
                if not text.lstrip().startswith("🔍 *Анализирую"):
                    messages.append(AIMessage(content=text[:400]))

        messages.append(
            HumanMessage(
                content=(
                    f"{summary_block}"
                    f"AST-КАРТА РЕПОЗИТОРИЯ:\n```\n{repo_map}\n```\n\n"
                    f"ЗАПРОС ПОЛЬЗОВАТЕЛЯ: {prompt}"
                )
            )
        )

        # 1. Основной режим: жесткий Structured Output на уровне сэмплера Ollama
        try:
            structured_llm = llm.with_structured_output(ExecutionPlan, method="json_schema")
            plan = await structured_llm.ainvoke(messages)
            if isinstance(plan, ExecutionPlan) and plan.steps:
                return plan
        except Exception:
            pass

        # 2. Запасной парсер, если модель вернула сырой JSON-текст
        raw_res = await llm.ainvoke(messages)
        match = re.search(r"\{.*\}", raw_res.content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                return ExecutionPlan.model_validate(data)
            except Exception:
                pass

        return ExecutionPlan(steps=[PlanStep(action="answer", instruction=raw_res.content)])

    async def generate_code(self, system_prompt: str, user_prompt: str) -> str:
        """Генерирует полный код или JSON-патч с расширенным окном контекста."""
        llm = self._build_llm(num_ctx=config.CODER_NUM_CTX)
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        response = await llm.ainvoke(messages)
        return response.content.strip()

    async def generate_patch(self, system_prompt: str, user_prompt: str) -> CodePatch | None:
        """Генерирует точечный патч по строгой JSON-схеме без ошибок экранирования."""
        llm = self._build_llm(num_ctx=config.CODER_NUM_CTX)
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        try:
            structured_llm = llm.with_structured_output(CodePatch, method="json_schema")
            patch = await structured_llm.ainvoke(messages)
            if isinstance(patch, CodePatch) and patch.old_code:
                return patch
        except Exception:
            pass
        return None

    async def update_rolling_summary(self, old_summary: str, recent_messages: List[Tuple[str, str]]) -> str:
        """Сжимает последние сообщения и старый паспорт в компактный конспект."""
        llm = self._build_llm(num_ctx=config.PLANNER_NUM_CTX)
        dialog_text = "\n".join(f"{r.upper()}: {t[:500]}" for r, t in recent_messages)
        messages = [
            SystemMessage(content=config.SUMMARY_PROMPT),
            HumanMessage(
                content=(
                    f"ТЕКУЩИЙ ПАСПОРТ:\n{old_summary or '(пусто)'}\n\n"
                    f"НОВЫЕ СООБЩЕНИЯ:\n{dialog_text}"
                )
            ),
        ]
        res = await llm.ainvoke(messages)
        return res.content.strip()

    async def generate_answer(
        self,
        user_prompt: str,
        docs_context: str = "",
        recent_history: List[Tuple[str, str]] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Потоково генерирует развёрнутый ответ на русском языке с примерами кода в Markdown."""
        llm = self._build_llm(num_ctx=config.CODER_NUM_CTX)
        messages = [SystemMessage(content=config.ANSWER_PROMPT)]

        for role, text in (recent_history or []):
            if role == "user":
                messages.append(HumanMessage(content=text))
            elif role == "assistant" and not text.lstrip().startswith("🔍 *Анализирую"):
                messages.append(AIMessage(content=text[:600]))

        docs_block = f"НАЙДЕННАЯ ДОКУМЕНТАЦИЯ:\n{docs_context}\n\n" if docs_context else ""
        messages.append(HumanMessage(content=f"{docs_block}ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n{user_prompt}"))

        async for chunk in llm.astream(messages):
            if chunk.content:
                yield str(chunk.content)
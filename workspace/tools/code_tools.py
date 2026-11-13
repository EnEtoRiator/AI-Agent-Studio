import ast
import subprocess
from pathlib import Path
from typing import Tuple

import workspace.config as config

IGNORE_DIRS = {".git", "__pycache__", "venv", ".venv", ".idea", ".vscode", "build", "dist", "node_modules"}


class ProjectWorkspace:
    def __init__(self, root_path: str = config.DEFAULT_WORKSPACE):
        self.root = Path(root_path).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def set_root(self, new_path: str) -> Path:
        self.root = Path(new_path).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def resolve_safe_path(self, rel_path: str) -> Path | None:
        """Защита от выхода за пределы выбранного проекта (Path Traversal)."""
        if not rel_path:
            return self.root
        target = (self.root / rel_path).resolve()
        if self.root in target.parents or target == self.root:
            return target
        return None

    def list_project_tree(self) -> str:
        if not self.root.exists():
            return "Ошибка: Рабочая директория не существует."

        lines = []
        for item in sorted(self.root.rglob("*")):
            if any(part in IGNORE_DIRS for part in item.parts):
                continue
            rel = item.relative_to(self.root)
            prefix = "📁 " if item.is_dir() else "📄 "
            lines.append(f"{prefix}{rel}")

        return "\n".join(lines[:120]) if lines else "Проект пуст."

    def generate_repo_map(self, max_files: int = 40) -> str:
        """Строит компактную AST-карту классов, методов и функций всего репозитория."""
        if not self.root.exists():
            return "Рабочая директория не найдена."

        py_files = [
            p for p in sorted(self.root.rglob("*.py"))
            if not any(part in IGNORE_DIRS for part in p.parts)
        ][:max_files]

        if not py_files:
            return self.list_project_tree()

        map_blocks = []
        for py_file in py_files:
            rel_path = py_file.relative_to(self.root)
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(rel_path))
            except Exception as e:
                map_blocks.append(f"📄 {rel_path} (ошибка AST: {e})")
                continue

            signatures = []
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    bases = [ast.unparse(b) for b in node.bases]
                    base_str = f"({', '.join(bases)})" if bases else ""
                    signatures.append(f"  class {node.name}{base_str}:")
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            sig = self._format_func_signature(sub)
                            signatures.append(f"    {sig}")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sig = self._format_func_signature(node)
                    signatures.append(f"  {sig}")
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    ann = ast.unparse(node.annotation)
                    preview = self._preview_assign_value(node.value)
                    signatures.append(f"  {node.target.id}: {ann}{preview}")
                elif isinstance(node, ast.Assign):
                    targets = [ast.unparse(t) for t in node.targets]
                    preview = self._preview_assign_value(node.value)
                    signatures.append(f"  {', '.join(targets)}{preview}")

            if signatures:
                map_blocks.append(f"📄 {rel_path}:\n" + "\n".join(signatures))
            else:
                map_blocks.append(f"📄 {rel_path} (модуль без классов/функций)")

        return "\n\n".join(map_blocks)

    def _format_func_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        args = []
        for arg in node.args.args:
            ann = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
            args.append(f"{arg.arg}{ann}")
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        doc = ast.get_docstring(node)
        doc_hint = f"  # {doc.splitlines()[0][:60]}" if doc else ""
        return f"{prefix} {node.name}({', '.join(args)}){ret}{doc_hint}"

    @staticmethod
    def _preview_assign_value(value: ast.expr | None) -> str:
        if value is None:
            return ""
        if isinstance(value, ast.Dict):
            keys = [ast.unparse(k) for k in value.keys if k is not None][:10]
            return f" = {{{', '.join(keys)}: ...}}"
        raw = ast.unparse(value)
        return f" = {raw[:60]}" if len(raw) <= 60 else f" = {raw[:57]}..."

    def read_source_file(self, file_path: str) -> str:
        target = self.resolve_safe_path(file_path)
        if not target or not target.is_file():
            return f"Ошибка: Файл '{file_path}' не найден."
        try:
            return target.read_text(encoding="utf-8")
        except Exception as e:
            return f"Ошибка чтения '{file_path}': {str(e)}"

    def write_source_file(self, file_path: str, new_content: str) -> Tuple[bool, str]:
        target = self.resolve_safe_path(file_path)
        if not target:
            return False, "Ошибка: Недопустимый путь за пределами проекта."
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_content, encoding="utf-8")
            return True, f"Файл '{file_path}' успешно сохранен."
        except Exception as e:
            return False, f"Ошибка записи '{file_path}': {str(e)}"

    def apply_patch_in_memory(self, original_content: str, old_code: str, new_code: str) -> Tuple[bool, str]:
        if not old_code or old_code not in original_content:
            return False, "Фрагмент old_code не найден в файле (проверьте отступы и точное совпадение)."
        return True, original_content.replace(old_code, new_code, 1)

    @staticmethod
    def validate_python_syntax(code: str, filename: str = "<string>") -> Tuple[bool, str]:
        try:
            ast.parse(code, filename=filename)
            return True, "OK"
        except SyntaxError as e:
            return False, f"SyntaxError в {filename} (строка {e.lineno}): {e.msg} -> {e.text}"

    def run_verification_command(self, command: str, timeout: int = 15) -> Tuple[bool, str]:
        """Запускает тестовый скрипт или команду проверки внутри директории проекта."""
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (proc.stdout + "\n" + proc.stderr).strip()
            # Считаем ошибкой ненулевой код возврата или явные маркеры провала тестов
            failed_markers = ("❌", "FAILED", "AssertionError", "Traceback (most recent call last)")
            is_ok = proc.returncode == 0 and not any(m in output for m in failed_markers)
            return is_ok, output or "(пустой вывод)"
        except subprocess.TimeoutExpired:
            return False, f"Таймаут выполнения команды ({timeout} сек)."
        except Exception as e:
            return False, f"Ошибка запуска команды: {str(e)}"
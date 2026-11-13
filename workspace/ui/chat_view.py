# workspace/ui/chat_view.py
import asyncio
from typing import Callable, Dict, List, Tuple, Any
import flet as ft

import workspace.config as config
from workspace.core.database import Database
from workspace.core.orchestrator import TaskOrchestrator


class ChatView(ft.Container):
    def __init__(
        self,
        orchestrator: TaskOrchestrator,
        db: Database,
        on_chat_updated: Callable,
    ):
        super().__init__()
        self.expand = True
        self.padding = ft.Padding.all(16)
        self.orchestrator = orchestrator
        self.db = db
        self.on_chat_updated = on_chat_updated

        self.project_id: int = 1
        self.chat_id: int = 1

        # Состояние активных фоновых генераций по chat_id:
        # {chat_id: {"task": asyncio.Task, "buffer": List[str], "md_widget": ft.Markdown}}
        self._active_generations: Dict[int, Dict[str, Any]] = {}

        self.messages_list = ft.ListView(
            expand=True,
            spacing=12,
            auto_scroll=True,
        )

        self.input_field = ft.TextField(
            hint_text="Поставьте задачу по коду или задайте вопрос... (Enter — отправить, Shift+Enter — перенос)",
            expand=True,
            multiline=True,
            min_lines=1,
            max_lines=5,
            shift_enter=True,
            on_submit=self._handle_action_button,
        )

        self.action_button = ft.IconButton(
            icon=ft.Icons.ARROW_UPWARD_ROUNDED,
            icon_color=ft.Colors.ON_PRIMARY,
            bgcolor=ft.Colors.PRIMARY,
            tooltip="Отправить (Enter)",
            on_click=self._handle_action_button,
        )

        self.content = ft.Column(
            controls=[
                self.messages_list,
                ft.Row(
                    controls=[self.input_field, self.action_button],
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
            ],
            expand=True,
        )

    def _set_generating_ui(self, is_generating: bool) -> None:
        """Переключает кнопку между режимами Отправить / Остановить для текущего чата."""
        self.input_field.disabled = is_generating
        if is_generating:
            self.action_button.icon = ft.Icons.STOP_ROUNDED
            self.action_button.bgcolor = ft.Colors.ERROR
            self.action_button.icon_color = ft.Colors.ON_ERROR
            self.action_button.tooltip = "Остановить генерацию"
        else:
            self.action_button.icon = ft.Icons.ARROW_UPWARD_ROUNDED
            self.action_button.bgcolor = ft.Colors.PRIMARY
            self.action_button.icon_color = ft.Colors.ON_PRIMARY
            self.action_button.tooltip = "Отправить (Enter)"

    def _create_message_bubble(self, role: str, content: str = "") -> tuple[ft.Container, ft.Markdown]:
        is_user = role == "user"
        md_widget = ft.Markdown(
            value=content,
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
        )

        bubble = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text(
                        "Вы" if is_user else "AI Architect",
                        weight=ft.FontWeight.BOLD,
                        size=11,
                        color=ft.Colors.BLUE_300 if is_user else ft.Colors.GREEN_300,
                    ),
                    md_widget,
                ],
                tight=True,
            ),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST if is_user else ft.Colors.SURFACE_CONTAINER_HIGH,
            border_radius=8,
            padding=12,
            margin=ft.Margin.only(
                left=60 if is_user else 0,
                right=0 if is_user else 60,
            ),
        )
        return bubble, md_widget

    def load_session(self, project_id: int, chat_id: int, messages: List[Tuple[str, str]]) -> None:
        """Загружает чат из SQLite и подхватывает активную потоковую генерацию, если она идёт."""
        self.project_id = project_id
        self.chat_id = chat_id
        self.messages_list.controls.clear()

        for role, content in messages:
            bubble, _ = self._create_message_bubble(role, content)
            self.messages_list.controls.append(bubble)

        # Если для этого чата прямо сейчас идёт фоновая генерация — восстанавливаем живой блок
        active_state = self._active_generations.get(chat_id)
        if active_state:
            live_text = "".join(active_state["buffer"])
            bot_bubble, new_md = self._create_message_bubble("assistant", live_text)
            active_state["md_widget"] = new_md
            self.messages_list.controls.append(bot_bubble)
            self._set_generating_ui(True)
        else:
            self._set_generating_ui(False)

        if self.page:
            self.page.update()

    async def _handle_action_button(self, e):
        # Если в текущем чате идёт генерация — останавливаем её
        if self.chat_id in self._active_generations:
            self._stop_generation(self.chat_id)
            return

        await self._start_generation()

    def _stop_generation(self, target_chat_id: int) -> None:
        state = self._active_generations.get(target_chat_id)
        if state and not state["task"].done():
            state["task"].cancel()

    async def _start_generation(self) -> None:
        text = self.input_field.value.strip()
        if not text:
            return

        target_project_id = self.project_id
        target_chat_id = self.chat_id

        self.input_field.value = ""
        self._set_generating_ui(True)

        user_bubble, _ = self._create_message_bubble("user", text)
        self.messages_list.controls.append(user_bubble)

        bot_bubble, bot_md = self._create_message_bubble("assistant", "")
        self.messages_list.controls.append(bot_bubble)
        self.page.update()

        buffer: List[str] = []
        task = asyncio.create_task(
            self._run_generation_task(
                target_project_id=target_project_id,
                target_chat_id=target_chat_id,
                user_prompt=text,
                buffer=buffer,
            )
        )
        self._active_generations[target_chat_id] = {
            "task": task,
            "buffer": buffer,
            "md_widget": bot_md,
        }

    async def _run_generation_task(
        self,
        target_project_id: int,
        target_chat_id: int,
        user_prompt: str,
        buffer: List[str],
    ) -> None:
        try:
            async for chunk in self.orchestrator.execute_task(
                project_id=target_project_id,
                chat_id=target_chat_id,
                user_prompt=user_prompt,
            ):
                buffer.append(chunk)
                if not buffer[0].startswith("🔍"):
                    pass
                # Обновляем заголовок в сайдбаре сразу после первого чанка
                if len(buffer) == 1:
                    self.on_chat_updated()

                # Обновляем UI только если пользователь сейчас смотрит этот чат
                state = self._active_generations.get(target_chat_id)
                if state and self.chat_id == target_chat_id:
                    state["md_widget"].value = "".join(buffer)
                    self.page.update()

            self.on_chat_updated()
            await self._maybe_update_summary(target_project_id, target_chat_id)

        except asyncio.CancelledError:
            buffer.append("\n\n⏹ *Генерация остановлена пользователем.*")
            partial_text = "".join(buffer)
            self.db.add_message(target_chat_id, "assistant", partial_text)

            state = self._active_generations.get(target_chat_id)
            if state and self.chat_id == target_chat_id:
                state["md_widget"].value = partial_text
                self.page.update()

        except Exception as ex:
            err_text = f"\n\n⚠️ **Ошибка выполнения:** {str(ex)}"
            buffer.append(err_text)
            self.db.add_message(target_chat_id, "assistant", "".join(buffer))

            state = self._active_generations.get(target_chat_id)
            if state and self.chat_id == target_chat_id:
                state["md_widget"].value = "".join(buffer)
                self.page.update()

        finally:
            self._active_generations.pop(target_chat_id, None)
            if self.chat_id == target_chat_id:
                self._set_generating_ui(False)
                self.page.update()
                await self.input_field.focus()

    async def _maybe_update_summary(self, project_id: int, chat_id: int) -> None:
        total = self.db.count_chat_messages(chat_id)
        if total > 0 and total % config.SUMMARY_INTERVAL == 0:
            old_summary = self.db.get_project_summary(project_id)
            recent = self.db.get_recent_messages(chat_id, limit=config.SUMMARY_INTERVAL)
            new_summary = await self.orchestrator.model.update_rolling_summary(old_summary, recent)
            if new_summary:
                self.db.update_project_summary(project_id, new_summary)
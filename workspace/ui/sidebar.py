# workspace/ui/sidebar.py
from pathlib import Path
from typing import Callable, List, Dict, Any
import flet as ft

import workspace.config as config


class Sidebar(ft.Container):
    def __init__(
        self,
        on_new_chat: Callable,
        on_select_chat: Callable[[int], None],
        on_delete_chat: Callable[[int], None],
        on_pick_folder: Callable,
        on_change_model: Callable[[str], None],
    ):
        super().__init__()
        self.width = 270
        self.bgcolor = ft.Colors.SURFACE_CONTAINER_LOW
        self.padding = ft.Padding.all(12)

        self.on_new_chat = on_new_chat
        self.on_select_chat = on_select_chat
        self.on_delete_chat = on_delete_chat
        self.on_pick_folder = on_pick_folder
        self.on_change_model = on_change_model

        # Отображение текущей рабочей папки
        self.project_name_text = ft.Text(
            Path(config.DEFAULT_WORKSPACE).name,
            size=13,
            weight=ft.FontWeight.BOLD,
            expand=True,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )

        self.folder_btn = ft.IconButton(
            icon=ft.Icons.FOLDER_OPEN_ROUNDED,
            icon_size=18,
            tooltip="Выбрать папку проекта",
            on_click=self._handle_pick_folder,
        )

        # Кнопка нового диалога
        self.new_chat_btn = ft.Button(
            content="Новый чат",
            icon=ft.Icons.ADD_ROUNDED,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
            width=246,
            on_click=self._handle_new_chat,
        )

        # Список сохраненных чатов из SQLite
        self.chat_history_list = ft.ListView(
            expand=True,
            spacing=4,
            controls=[],
        )

        # Роутер моделей (3B GPU / 7B CPU+RAM)
        self.model_dropdown = ft.Dropdown(
            value=config.DEFAULT_PROFILE,
            options=[
                ft.dropdown.Option(key=k, text=v["label"])
                for k, v in config.MODEL_PROFILES.items()
            ],
            text_size=12,
            dense=True,
            border=ft.OutlineInputBorder(border_radius=8),
            on_select=self._handle_model_change,
        )

        self.content = ft.Column(
            controls=[
                # Блок активного проекта
                ft.Container(
                    content=ft.Row(
                        controls=[
                            ft.Icon(ft.Icons.TERMINAL_ROUNDED, size=18, color=ft.Colors.PRIMARY),
                            self.project_name_text,
                            self.folder_btn,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                    border_radius=8,
                    padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                ),
                self.new_chat_btn,
                ft.Divider(height=8, color=ft.Colors.OUTLINE_VARIANT),
                ft.Text(
                    "ИСТОРИЯ ЧАТОВ",
                    size=11,
                    weight=ft.FontWeight.BOLD,
                    color=ft.Colors.GREY_400,
                ),
                self.chat_history_list,
                ft.Divider(height=8, color=ft.Colors.OUTLINE_VARIANT),
                ft.Text(
                    "РЕЖИМ ИНФЕРЕНСА",
                    size=11,
                    weight=ft.FontWeight.BOLD,
                    color=ft.Colors.GREY_400,
                ),
                self.model_dropdown,
            ],
            expand=True,
            spacing=8,
        )

    def update_project_label(self, folder_path: str) -> None:
        self.project_name_text.value = Path(folder_path).name or folder_path
        if self.page:
            self.page.update()

    def render_chats(self, chats: List[Dict[str, Any]], active_chat_id: int) -> None:
        self.chat_history_list.controls.clear()
        for chat in chats:
            cid = chat["id"]
            is_selected = cid == active_chat_id
            tile = ft.ListTile(
                leading=ft.Icon(
                    ft.Icons.CHAT_BUBBLE_ROUNDED if is_selected else ft.Icons.CHAT_BUBBLE_OUTLINE,
                    size=16,
                    color=ft.Colors.PRIMARY if is_selected else ft.Colors.GREY_400,
                ),
                title=ft.Text(
                    chat["title"],
                    size=12,
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                trailing=ft.IconButton(
                    icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                    icon_size=15,
                    tooltip="Удалить чат",
                    on_click=lambda e, target_id=cid: self.on_delete_chat(target_id),
                ),
                selected=is_selected,
                dense=True,
                on_click=lambda e, target_id=cid: self.on_select_chat(target_id),
            )
            self.chat_history_list.controls.append(tile)

        if self.page:
            self.page.update()

    async def _handle_pick_folder(self, e):
        if self.on_pick_folder:
            await self.on_pick_folder()

    def _handle_new_chat(self, e):
        if self.on_new_chat:
            self.on_new_chat()

    def _handle_model_change(self, e):
        if self.on_change_model and self.model_dropdown.value:
            self.on_change_model(self.model_dropdown.value)
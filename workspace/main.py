import os
import sys
from pathlib import Path

os.environ["GDK_BACKEND"] = "x11"  # Защита от сбоев Wayland на Ubuntu

# Добавляем родительский каталог в sys.path для корректных импортов workspace.*
ROOT_PARENT = str(Path(__file__).resolve().parent.parent)
if ROOT_PARENT not in sys.path:
    sys.path.insert(0, ROOT_PARENT)

import flet as ft
import workspace.config as config
from workspace.core.database import Database
from workspace.core.model import ModelService
from workspace.core.orchestrator import TaskOrchestrator
from workspace.tools.code_tools import ProjectWorkspace
from workspace.tools.doc_search import DocSearchEngine
from workspace.ui.sidebar import Sidebar
from workspace.ui.chat_view import ChatView


async def main(page: ft.Page):
    page.title = "AI Agent Studio"
    page.theme_mode = ft.ThemeMode.DARK
    page.window.width = 1050
    page.window.height = 660
    page.padding = 0

    # 1. Инициализация ядра: БД, рабочее пространство, поиск по докам, модели и оркестратор
    db = Database()
    workspace = ProjectWorkspace(config.DEFAULT_WORKSPACE)
    doc_search = DocSearchEngine(db=db, docs_dir=config.DOCS_DIR)
    model_service = ModelService(profile_key=config.DEFAULT_PROFILE)
    orchestrator = TaskOrchestrator(
        model_service=model_service,
        db=db,
        workspace=workspace,
        doc_search=doc_search,
    )

    # Состояние активного проекта и чата
    state = {
        "project": db.get_or_create_project(config.DEFAULT_WORKSPACE),
        "chat_id": None,
    }

    # Диалог выбора директории проекта
    file_picker = ft.FilePicker()
    if hasattr(page, "services"):
        page.services.append(file_picker)
    else:
        page.overlay.append(file_picker)

    def refresh_sidebar():
        chats = db.list_chats(state["project"]["id"])
        sidebar.render_chats(chats, state["chat_id"])

    def open_chat(chat_id: int):
        state["chat_id"] = chat_id
        msgs = db.get_chat_messages(chat_id)
        chat_view.load_session(state["project"]["id"], chat_id, msgs)
        refresh_sidebar()

    def create_new_chat():
        new_chat = db.create_chat(state["project"]["id"], title="Новый диалог")
        open_chat(new_chat["id"])

    def delete_chat(chat_id: int):
        db.delete_chat(chat_id)
        chats = db.list_chats(state["project"]["id"])
        if chats:
            open_chat(chats[0]["id"])
        else:
            create_new_chat()

    async def pick_project_folder():
        selected_dir = await file_picker.get_directory_path(dialog_title="Выберите папку проекта")
        if selected_dir:
            workspace.set_root(selected_dir)
            state["project"] = db.get_or_create_project(selected_dir)
            sidebar.update_project_label(selected_dir)
            chats = db.list_chats(state["project"]["id"])
            if chats:
                open_chat(chats[0]["id"])
            else:
                create_new_chat()

    def change_model_profile(profile_key: str):
        model_service.set_profile(profile_key)

    # 2. Инициализация UI
    chat_view = ChatView(
        orchestrator=orchestrator,
        db=db,
        on_chat_updated=refresh_sidebar,
    )

    sidebar = Sidebar(
        on_new_chat=create_new_chat,
        on_select_chat=open_chat,
        on_delete_chat=delete_chat,
        on_pick_folder=pick_project_folder,
        on_change_model=change_model_profile,
    )

    layout = ft.Row(
        controls=[
            sidebar,
            ft.VerticalDivider(width=1, color=ft.Colors.OUTLINE_VARIANT),
            chat_view,
        ],
        expand=True,
        spacing=0,
    )

    page.add(layout)

    # 3. Загрузка первого чата при старте
    existing_chats = db.list_chats(state["project"]["id"])
    if existing_chats:
        open_chat(existing_chats[0]["id"])
    else:
        create_new_chat()

    await chat_view.input_field.focus()


if __name__ == "__main__":
    ft.run(main)
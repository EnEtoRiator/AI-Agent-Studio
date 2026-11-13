# test_tools.py
from langchain_ollama import ChatOllama
from workspace.tools.code_tools import CODE_TOOLS

llm = ChatOllama(model="qwen2.5-coder:3b", temperature=0)
llm_with_tools = llm.bind_tools(CODE_TOOLS)

res = llm_with_tools.invoke("Выведи список файлов проекта")

print("--- ЧТО В ПОЛЕ CONTENT (текст): ---")
print(res.content)
print("\n--- ЧТО В ПОЛЕ TOOL_CALLS (реальные вызовы): ---")
print(res.tool_calls)
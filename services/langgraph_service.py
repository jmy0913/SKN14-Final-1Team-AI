from models.chat_model import ChatRequest2

from services.utils.langgraph_setting2 import graph_setting
import traceback

graph = graph_setting()

async def run_langraph(request: ChatRequest2) -> str:
        user_input = request.user_input
        config_id = request.config_id
        image = request.image
        chat_history = request.chat_history

        try:
            config = {"configurable": {"thread_id": config_id}}

            # chat_history가 None이면 빈 리스트로 초기화
            if chat_history is None:
                chat_history = []

            print(f"run_langraph 호출 - 입력: {user_input}, 이미지: {bool(image)}")

            result = graph.invoke(
                {
                    "messages": chat_history,
                    "question": user_input,
                    "image": image,
                    "retry": False,
                },
                config=config,
            )

            return result["answer"]


        except Exception as e:

            print(f"run_langraph 에러: {str(e)}")
            traceback.print_exc()
            return f"처리 중 오류가 발생했습니다: {str(e)}"

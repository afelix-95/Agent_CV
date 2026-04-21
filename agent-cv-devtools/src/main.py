import asyncio
import os
import re

import httpx
from microsoft_teams.api import MessageActivity, TypingActivityInput
from microsoft_teams.apps import ActivityContext, App
from microsoft_teams.devtools import DevToolsPlugin

app = App(plugins=[DevToolsPlugin()])

BACKEND_QUERY_URL = os.getenv("BACKEND_QUERY_URL", "http://localhost:8000/query")
BACKEND_TIMEOUT_SECONDS = float(os.getenv("BACKEND_TIMEOUT_SECONDS", "20"))


async def _query_backend(query_text: str, conversation_id: str | None) -> tuple[str | None, str | None]:
    payload = {
        "query": query_text,
        "conversation_id": conversation_id,
    }
    try:
        async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT_SECONDS) as client:
            response = await client.post(BACKEND_QUERY_URL, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.TimeoutException:
        return None, "Backend timeout. Confirm the Agent CV API is running and reachable."
    except httpx.HTTPStatusError as exc:
        return None, f"Backend returned HTTP {exc.response.status_code}."
    except httpx.HTTPError as exc:
        return None, f"Failed to reach backend: {exc}"

    language = body.get("language", "en")
    summary = body.get("summary")
    answer = body.get("answer")
    has_more = bool(body.get("has_more", False))

    if not summary and not answer:
        return None, "Backend response did not include an answer."

    lines: list[str] = []
    if summary:
        lines.append(summary)
    if answer:
        if lines:
            lines.append("")
        lines.append(answer)

    if has_more:
        lines.append("")
        lines.append("Peça 'mostrar mais resultados' para continuar." if language == "pt" else "Ask 'show more results' to continue.")

    return "\n".join(lines), None


@app.on_message_pattern(re.compile(r"^\s*(hello|hi|greetings|ola|oi)\b", re.IGNORECASE))
async def handle_greeting(ctx: ActivityContext[MessageActivity]) -> None:
    """Handle greeting messages."""
    await ctx.send(
        "Hello! Ask me about certifications, for example: "
        "'Who has Red Hat certifications?' or 'Show expired Dell certifications'."
    )


@app.on_message
async def handle_message(ctx: ActivityContext[MessageActivity]):
    """Forward user queries to the Agent CV backend and return real results."""
    await ctx.reply(TypingActivityInput())
    user_text = (ctx.activity.text or "").strip()
    if not user_text:
        await ctx.send("Please enter a query.")
        return

    conversation = getattr(ctx.activity, "conversation", None)
    conversation_id = getattr(conversation, "id", None)
    result_text, error_text = await _query_backend(user_text, conversation_id)
    if error_text:
        await ctx.send(error_text)
        return

    await ctx.send(result_text)


def main():
    asyncio.run(app.start())


if __name__ == "__main__":
    main()

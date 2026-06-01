import asyncio
import anthropic
from telegram import Bot
from utils.config import settings

def check_anthropic():
    print("Checking Anthropic...")
    c = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    r = c.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=10,
        messages=[{"role": "user", "content": "say ok"}]
    )
    print(f"  Anthropic: {r.content[0].text}")

async def check_telegram():
    print("Checking Telegram...")
    b = Bot(settings.telegram_bot_token)
    await b.send_message(
        chat_id=settings.telegram_chat_id,
        text="PreMarket Pro setup verified! ✅"
    )
    print("  Telegram: message sent")

if __name__ == "__main__":
    check_anthropic()
    asyncio.run(check_telegram())
    print("\nAll checks passed. Ready to build.")
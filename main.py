import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from bot.admin import router as admin_router
from bot.visitor import router as visitor_router
from worker import pipeline
from webapp.app import create_app


async def main():
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(visitor_router)

    pipeline.LOG_CHANNEL_ID = int(os.environ["LOG_CHANNEL_ID"])
    pipeline.BOT_USERNAME = (await bot.get_me()).username

    await pipeline.recover_unfinished_jobs()
    worker_task = asyncio.create_task(pipeline.worker_loop(bot))

    webapp_port = int(os.environ.get("WEBAPP_PORT", "8097"))
    web_server = uvicorn.Server(
        uvicorn.Config(create_app(bot), host="0.0.0.0", port=webapp_port, log_level="info")
    )

    try:
        await asyncio.gather(dp.start_polling(bot), web_server.serve())
    finally:
        worker_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())

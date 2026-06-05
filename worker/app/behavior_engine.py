
import asyncio, random

from playwright.async_api import Page



class BehaviorEngine:

    @staticmethod

    async def human_like_move(page: Page, target_x: int, target_y: int):

        steps = random.randint(10, 25)

        prev_x, prev_y = 0, 0

        for i in range(1, steps + 1):

            t = i / steps

            cur_x = prev_x + (target_x - prev_x) * t + random.uniform(-5, 5)

            cur_y = prev_y + (target_y - prev_y) * t + random.uniform(-5, 5)

            await page.mouse.move(cur_x, cur_y)

            await asyncio.sleep(random.uniform(0.02, 0.08))



    @staticmethod

    async def random_delay(min_s=0.5, max_s=2.0):

        await asyncio.sleep(random.uniform(min_s, max_s))



    @staticmethod

    async def type_naturally(page: Page, text: str):

        for char in text:

            await page.keyboard.type(char, delay=random.randint(50, 150))

            if random.random() < 0.1:

                await asyncio.sleep(random.uniform(0.3, 0.6))

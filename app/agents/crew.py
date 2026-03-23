import asyncio
from functools import partial


async def run_crew_async(crew, inputs: dict) -> str:
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, partial(crew.kickoff, inputs=inputs))
    return str(result)

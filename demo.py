import asyncio

from wedo2 import WeDoHub


async def main():
    async with WeDoHub() as hub:
        hub.on_button = lambda pressed: pressed and print("Green button pressed!")

        # Light show
        for color in ["red", "green", "blue", "purple"]:
            await hub.led(color)
            await asyncio.sleep(0.5)

        await hub.beep(523, 200)
        await hub.beep(659, 200)
        await hub.beep(784, 300)

        # Motor on port 1: forward, then backward
        print("Motor forward...")
        await hub.motor(1, 60)
        await asyncio.sleep(2)
        print("Motor backward...")
        await hub.motor(1, -60)
        await asyncio.sleep(2)
        await hub.motor(1, 0)

        # Show sensor readings for 10 seconds
        print("Reading sensors for 10 seconds (move the tilt / distance sensor)...")
        for _ in range(20):
            print(f"  tilt={hub.tilt()}  distance={hub.distance()}")
            await asyncio.sleep(0.5)

        await hub.led("green")


asyncio.run(main())

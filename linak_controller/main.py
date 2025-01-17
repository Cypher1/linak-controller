#!/usr/bin/env python3
import os
import traceback
import asyncio
import aiohttp
from aiohttp import web
from bleak import BleakClient, BleakError, BleakScanner
import json
from functools import partial
from .config import config, Commands
from .util import Height
from .desk import Desk

ALLOWED_KEYS = ["command", "move_to", "quiet"]
ALLOWED_COMMANDS = [None, Commands.move_to, Commands.watch]

async def scan():
    """Scan for a bluetooth device with the configured address and return it or return all devices if no address specified"""
    print("Scanning\r", end="")
    devices = await BleakScanner().discover(
        device=config.adapter_name, timeout=config.scan_timeout
    )
    print("Found {} devices using {}".format(len(devices), config.adapter_name))
    for device in devices:
        print(device)
    return devices


def disconnect_callback(client, _=None):
    if not config.disconnecting:
        print("Lost connection with {}".format(client.address))
        asyncio.create_task(connect(client))


async def connect(client=None, attempt=0):
    """Attempt to connect to the desk"""
    print("Connecting\r", end="")
    if not client:
        client = BleakClient(
            config.mac_address,
            device=config.adapter_name,
            disconnected_callback=disconnect_callback,
        )
    await client.connect(timeout=config.connection_timeout)
    print("Connected {}".format(config.mac_address))

    await Desk.initialise(client)

    return client


async def disconnect(client):
    """Attempt to disconnect cleanly"""
    if client.is_connected:
        config.disconnecting = True
        await client.disconnect()


async def run_command(client: BleakClient):
    """Begin the action specified by command line arguments and config"""
    # Always print current height
    initial_height, _ = await Desk.get_height_speed(client)
    Desk.log_state(initial_height)
    target = None
    if config.command == Commands.watch:
        # Print changes to height data
        config.info("Watching for changes to desk height and speed")
        await Desk.watch_height_speed(client)
    elif config.command == Commands.move_to:
        # Move to custom height
        if config.move_to in config.favourites:
            target = Height(config.favourites.get(config.move_to), True)
            config.info(
                f"Moving to favourite height: {config.move_to} ({target.human} mm)"
            )
        elif str(config.move_to).isnumeric():
            target = Height(int(config.move_to), True)
            config.info(f"Moving to height: {config.move_to}")
        else:
            config.error(f"Not a valid height or favourite position: {config.move_to}")
            return
        if target.value == initial_height.value:
            config.warn(f"Nothing to do - already at specified height")
            return
        await Desk.move_to(client, target)
    if target:
        final_height, _ = await Desk.get_height_speed(client)
        # If we were moving to a target height, wait, then print the actual final height
        config.info(
            "Final height: {:4.0f}mm (Target: {:4.0f}mm)".format(
                final_height.human, target.human
            )
        )


async def run_tcp_server(client):
    """Start a simple tcp server to listen for commands"""

    server = await asyncio.start_server(
        partial(run_tcp_forwarded_command, client),
        config.server_address,
        config.server_port,
    )
    print("TCP Server listening")
    await server.serve_forever()


async def run_tcp_forwarded_command(client, reader, writer):
    """Run commands received by the tcp server"""
    print("Received command")
    request = (await reader.read()).decode("utf8")
    forwarded_config = json.loads(str(request))
    for key in forwarded_config:
        setattr(config, key, forwarded_config[key])
    await run_command(client)
    writer.close()


async def run_server(client: BleakClient):
    """Start a server to listen for commands via websocket connection"""
    app = web.Application()
    app.router.add_get("/", partial(run_forwarded_command, client))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.server_address, config.server_port)
    await site.start()
    print("Server listening")
    await asyncio.Future()


async def run_forwarded_command(client: BleakClient, request):
    """Run commands received by the server"""
    print("Received command")
    ws = web.WebSocketResponse()

    def log(message, end="\n"):
        print(message, end=end)
        asyncio.create_task(ws.send_str(str(message)))

    config.log = log

    await ws.prepare(request)
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            forwarded_config = json.loads(msg.data)
            for key in forwarded_config:
                setattr(config, key, forwarded_config[key])
            await run_command(client)
        break
    await asyncio.sleep(1)  # Allows final messages to send on web socket
    await ws.close()
    return ws


async def forward_command():
    """Send commands to a server instance of this script"""
    # TODO: Check these server side.
    if config.command not in ALLOWED_COMMANDS:
        print(f"Command must be one of {ALLOWED_COMMANDS}")
        return
    config_dict = config.__dict__
    # TODO: Check these server side.
    forwarded_config = {
        key: config_dict[key] for key in ALLOWED_KEYS if key in config_dict
    }
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(
        f"http://{config.server_address}:{config.server_port}"
    )
    await ws.send_str(json.dumps(forwarded_config))
    while True:
        msg = await ws.receive()
        if msg.type == aiohttp.WSMsgType.text:
            print(msg.data)
        elif msg.type in [aiohttp.WSMsgType.closed, aiohttp.WSMsgType.error]:
            break
    await ws.close()
    await session.close()


async def manage():
    """Set up the async event loop and signal handlers"""
    try:
        client = None
        # Forward and scan don't require a connection so run them and exit
        if config.forward:
            await forward_command()
            return 0
        elif config.command == Commands.scan_adapter:
            await scan()
            return 0
        
        # Server and other commands do require a connection so set one up
        try:
            client = await connect()
        except BleakError as e:
            print("Connecting failed")
            if "was not found" in str(e):
                print(e)
            else:
                print(traceback.format_exc())
            return 1
        except asyncio.exceptions.TimeoutError as e:
            print("Connecting failed - timed out")
            return 1
        if config.command == Commands.server:
            await run_server(client)
        elif config.command == Commands.tcp_server:
            await run_tcp_server(client)
        else:
            await run_command(client)
        return 0
    except OSError as e:
        print(e)
        return 1
    except Exception as e:
        print("\nSomething unexpected went wrong:")
        print(traceback.format_exc())
        return 1
    finally:
        if client:
            print("\rDisconnecting\r", end="")
            await Desk.stop(client)
            await disconnect(client)
            print("Disconnected         ")


async def main():
    return_code = await manage()
    while config.forever:
        return_code = await manage()
    os._exit(return_code)


def init():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    init()

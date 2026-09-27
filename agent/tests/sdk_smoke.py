"""Independent official MCP client check. CI installs the pinned client dependency."""
import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'relay'))
from server import App, Server


async def main():
    with tempfile.TemporaryDirectory() as temp:
        app = App(str(Path(temp)/'db.sqlite'), 'https://relay.example', 'ipad',
                  'd'*40, 'a'*40, 'client', 'c'*40, 'o'*40, ['https://client.example/callback'])
        server = Server(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            async with httpx.AsyncClient(headers={'Authorization':'Bearer '+'a'*40, 'Host':'relay.example'}) as http:
                async with streamable_http_client(f'http://127.0.0.1:{server.server_port}/mcp', http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        assert initialized.serverInfo.name == 'GhostBlender'
                        tools = await session.list_tools()
                        assert len(tools.tools) == 19
                        result = await session.call_tool('status', {})
                        assert not result.isError
                        assert 'device_not_paired' in result.content[0].text
            print('Official MCP client: initialize, discovery, tool call and shutdown passed')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            app.store.db.close()


if __name__ == '__main__':
    asyncio.run(main())

from fastapi import FastAPI
import logging
import threading
from queue import Queue
from typing import Any, Dict, Optional
from uvicorn import Config, Server

logger = logging.getLogger("ApiWrapper")

class ApiWrapper:
    """Run a FastAPI instance in a background thread and push POSTed JSON to a shared queue."""

    def __init__(self, shared_queue: Queue) -> None:
        self.app = FastAPI()
        self.shared_queue = shared_queue

        # Define a route to handle incoming POST requests in FastAPI
        @self.app.post('/post_listener')
        async def post_handler(data: Dict[str, Any]) -> Dict[str, str]:
            logger.debug("Received POST data: %s", data)
            self.shared_queue.put(data)
            return {'message': 'Data received successfully!'}

    def run_app(self, host: str = '127.0.0.1', port: int = 8889, stop_event: Optional[threading.Event] = None) -> None:
        """Serve the POST listener.

        Bound to loopback: anything posted here lands on the control queue and
        drives the menu, and the menu can reach shutdown/restart. On 0.0.0.0
        that is an unauthenticated power switch for anyone on the network.
        """
        # Use programmatic Server so we can stop it cleanly
        config = Config(app=self.app, host=host, port=port, log_level='info')
        server = Server(config=config)

        def _run():
            server.run()

        server_thread = threading.Thread(target=_run, daemon=True)
        server_thread.start()

        if stop_event is not None:
            stop_event.wait()
            server.should_exit = True
            # Bound the wait: uvicorn's graceful shutdown can stall on open
            # connections. The daemon thread is reaped on process exit anyway.
            server_thread.join(timeout=5)
        else:
            # Block until server stops if no stop_event provided
            server_thread.join()
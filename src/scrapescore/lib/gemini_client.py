"""Client for submitting requests to the Gemini queue via Redis."""

import json
import os
import time
import uuid

import redis
from dotenv import load_dotenv

from scrapescore.lib.config import APP_CONFIG

load_dotenv()



# Redis configuration — host/port/db from yaml; password from .env (secret)
REDIS_HOST = APP_CONFIG["redis"]["host"]
REDIS_PORT = int(APP_CONFIG["redis"]["port"])
REDIS_DB = int(APP_CONFIG["redis"]["db"])
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
REDIS_DECODE_RESPONSES = True

# Redis keys
REQUEST_QUEUE = "gemini:requests"
RESULT_PREFIX = "gemini:results"


class GeminiClient:
    """Client for submitting requests to the Gemini queue via Redis."""

    def __init__(
        self,
        redis_host: str = REDIS_HOST,
        redis_port: int = REDIS_PORT,
        redis_db: int = REDIS_DB,
        redis_password: str | None = REDIS_PASSWORD
    ):
        """
        Initialize the client.

        Args:
            redis_host: Redis host
            redis_port: Redis port
            redis_db: Redis database number
            redis_password: Redis password
        """
        self.redis = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_password,
            decode_responses=REDIS_DECODE_RESPONSES,
            socket_connect_timeout=3,
        )
        try:
            self.redis.ping()
        except (redis.ConnectionError, redis.TimeoutError) as e:
            raise ConnectionError(
                f"Cannot connect to Redis at {redis_host}:{redis_port}. "
                f"Ensure Redis is running. Error: {e}"
            ) from e

    def submit(self, prompt: str, request_id: str | None = None) -> str:
        """
        Submit a prompt to the queue via Redis.

        Args:
            prompt: The prompt to submit
            request_id: Optional request ID (generated if not provided)

        Returns:
            The request ID
        """
        if request_id is None:
            request_id = str(uuid.uuid4())

        data = json.dumps({
            "request_id": request_id,
            "prompt": prompt
        })
        self.redis.rpush(REQUEST_QUEUE, data)
        return request_id

    def get_response(self, request_id: str) -> dict | None:
        """
        Get the response for a request via Redis.

        Args:
            request_id: The request ID

        Returns:
            dict with the result or None if not ready
        """
        key = f"{RESULT_PREFIX}:{request_id}"
        data = self.redis.hget(key, "response")
        if data:
            return json.loads(data)
        return None

    def wait_for_response(
        self,
        request_id: str,
        timeout: int = 120,
        poll_interval: float = 1.0
    ) -> dict:
        """
        Wait for a response to complete.

        Args:
            request_id: The request ID
            timeout: Maximum time to wait in seconds
            poll_interval: Time between polls in seconds

        Returns:
            dict with the result

        Raises:
            TimeoutError: If timeout is reached before response is ready
        """
        start = time.time()

        while time.time() - start < timeout:
            response = self.get_response(request_id)

            if response:
                return response

            time.sleep(poll_interval)

        raise TimeoutError(f"Timeout waiting for response {request_id}")

    def submit_and_wait(self, prompt: str, timeout: int = 120) -> dict:
        """
        Submit a prompt and wait for the response.

        Args:
            prompt: The prompt to submit
            timeout: Maximum time to wait in seconds

        Returns:
            dict with the result

        Raises:
            TimeoutError: If timeout is reached before response is ready
        """
        request_id = self.submit(prompt)
        return self.wait_for_response(request_id, timeout=timeout)


def main():
    """Test the client with a simple prompt."""
    import typer

    def cli(
        prompt: str = typer.Option("What is 2+2?", help="Prompt to send"),
        timeout: int = typer.Option(120, help="Timeout in seconds"),
        redis_host: str = typer.Option(REDIS_HOST, help="Redis host"),
        redis_port: int = typer.Option(REDIS_PORT, help="Redis port"),
    ):
        client = GeminiClient(
            redis_host=redis_host,
            redis_port=redis_port
        )

        print(f"Submitting prompt to Redis ({redis_host}:{redis_port})...")
        print(f"Prompt: {prompt}")

        try:
            response = client.submit_and_wait(prompt, timeout=timeout)

            if response["status"] == "success":
                print("\n=== SUCCESS ===")
                if response.get("result"):
                    print("Parsed Result:")
                    print(json.dumps(response["result"], indent=2))
                if response.get("raw_text"):
                    print("\nRaw Response:")
                    print(response["raw_text"])
            else:
                print(f"\n=== ERROR ===")
                print(response.get("error"))

        except TimeoutError:
            print(f"\n=== TIMEOUT ===")
            print(f"Request timed out after {timeout} seconds")
        except Exception as e:
            print(f"\n=== EXCEPTION ===")
            print(f"Error: {e}")

    typer.run(cli)


if __name__ == "__main__":
    main()
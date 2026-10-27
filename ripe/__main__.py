"""Development entrypoint; loopback by default, no public preview."""
import os
import uvicorn

if __name__ == '__main__':
    uvicorn.run('ripe.app:app',host=os.getenv('RIPE_HOST','127.0.0.1'),port=int(os.getenv('RIPE_PORT','8087')),access_log=False)

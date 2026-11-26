# start_dashboard.py
"""Install this project's requirements in its venv, start the server and open a browser."""
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen
import venv
import webbrowser


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("Install Python 3.11 or newer from python.org and launch again.")
    root = Path(__file__).resolve().parent
    os.chdir(root)
    port = int(os.environ.get("PORT", "8810"))
    url = f"http://localhost:{port}"

    def ready():
        try:
            with urlopen(url + "/healthz", timeout=.4) as response:
                return response.read(128).strip() == b'{"status": "ok"}'
        except OSError:
            return False

    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        venv.create(root / ".venv", with_pip=True)
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                    "-r", "requirements.txt"], check=True)
    if ready():
        webbrowser.open(url)
        return
    server = subprocess.Popen([str(python), "-m", "web.app"])
    try:
        while server.poll() is None:
            if ready():
                print(f"Paper terminal ready at {url}. Close this window to stop it.", flush=True)
                webbrowser.open(url)
                break
            time.sleep(.25)
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait()
    if server.returncode and server.returncode > 0:
        raise SystemExit(server.returncode)


if __name__ == "__main__":
    main()

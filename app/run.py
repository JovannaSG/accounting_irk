import os
import sys
import time
import subprocess


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_python_executable():
    # Detect virtual environment python
    if os.name == "nt":  # Windows
        venv_py = os.path.join(_PROJECT_ROOT, ".venv", "Scripts", "python.exe")
    else:  # Unix
        venv_py = os.path.join(_PROJECT_ROOT, ".venv", "bin", "python")

    if os.path.exists(venv_py):
        return venv_py
    return sys.executable


def main():
    os.chdir(_PROJECT_ROOT)
    py_bin = get_python_executable()

    api_host = os.environ.get("API_HOST", "127.0.0.1")
    api_port = int(os.environ.get("API_PORT", "8000"))
    ui_host = os.environ.get("UI_HOST", "127.0.0.1")
    ui_port = int(os.environ.get("UI_PORT", "8501"))

    print(f"Используется Python: {py_bin}")

    # 1. Start FastAPI server
    print(f"Запуск FastAPI-сервера (порт {api_port})...")
    server_cmd = [
        py_bin, "-m", "uvicorn", "app.server:app",
        "--host", api_host, "--port", str(api_port)
    ]
    server_proc = subprocess.Popen(server_cmd)

    # Wait a bit for the server to spin up
    time.sleep(2)

    # 2. Start Streamlit client
    print(f"Запуск Streamlit-интерфейса (порт {ui_port})...")
    client_cmd = [
        py_bin, "-m", "streamlit", "run", os.path.join("app", "ui.py"),
        "--server.port", str(ui_port), "--server.address", ui_host
    ]
    client_proc = subprocess.Popen(client_cmd)

    print("\nПриложение успешно запущено!")
    print(f"Адрес API: http://{api_host}:{api_port}")
    print(f"Адрес UI:  http://{ui_host}:{ui_port}")
    print("Для завершения нажмите Ctrl+C\n")

    try:
        while True:
            # Check if any process has exited unexpectedly
            if server_proc.poll() is not None:
                print("Сервер API неожиданно остановился.")
                break
            if client_proc.poll() is not None:
                print("Интерфейс Streamlit неожиданно остановился.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nЗавершение работы процессов...")
    finally:
        # Gracefully terminate processes
        try:
            client_proc.terminate()
            server_proc.terminate()
            client_proc.wait(timeout=3)
            server_proc.wait(timeout=3)
        except Exception:
            pass
        print("Процессы успешно завершены.")


if __name__ == "__main__":
    main()

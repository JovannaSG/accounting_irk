import os
import sys
import time
import subprocess
import signal

def get_python_executable():
    # Detect virtual environment python
    if os.name == "nt": # Windows
        venv_py = os.path.join(".venv", "Scripts", "python.exe")
    else: # Unix
        venv_py = os.path.join(".venv", "bin", "python")
        
    if os.path.exists(venv_py):
        return venv_py
    return sys.executable

def main():
    py_bin = get_python_executable()
    print(f"Используется Python: {py_bin}")
    
    # 1. Start FastAPI server
    print("Запуск FastAPI-сервера (порт 8000)...")
    server_cmd = [py_bin, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8000"]
    server_proc = subprocess.Popen(server_cmd)
    
    # Wait a bit for the server to spin up
    time.sleep(2)
    
    # 2. Start Streamlit client
    print("Запуск Streamlit-интерфейса (порт 8501)...")
    client_cmd = [py_bin, "-m", "streamlit", "run", "app.py", "--server.port", "8501", "--server.address", "127.0.0.1"]
    client_proc = subprocess.Popen(client_cmd)
    
    print("\nПриложение успешно запущено!")
    print("Адрес API: http://127.0.0.1:8000")
    print("Адрес UI:  http://127.0.0.1:8501")
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

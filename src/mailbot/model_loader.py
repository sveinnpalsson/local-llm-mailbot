import subprocess
from config import MODEL_PATH, LLAMA_CLI_PATH

def load_model():
    def generate(prompt: str, max_new_tokens: int = 32):
        cmd = [
            LLAMA_CLI_PATH,
            "-m", MODEL_PATH,
            "-c", "512",
            "-ngl", "99",
            "-t", "4",
            "-n", str(max_new_tokens),
            "-p", prompt
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.stdout.strip()
    return generate

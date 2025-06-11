import subprocess

def send_telegram(text, mode=None):
    """
    Only send if importance >= threshold.
    Includes suggested action in the message.
    """
    
    command = ['python', 'telegram_message.py', text]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, universal_newlines=True)
    stdout, stderr = process.communicate()


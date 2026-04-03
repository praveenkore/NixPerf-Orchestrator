import asyncore
import smtpd
import threading
import time

class MockSMTPServer(smtpd.SMTPServer):
    def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
        print(f"Message received from: {mailfrom}")
        print(f"To: {rcpttos}")
        print(f"Data: {data}")

def run_server():
    server = MockSMTPServer(('127.0.0.1', 1025), None)
    try:
        asyncore.loop()
    except Exception:
        pass

if __name__ == "__main__":
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    print("Mock SMTP server started on 127.0.0.1:1025")
    while True:
        time.sleep(1)

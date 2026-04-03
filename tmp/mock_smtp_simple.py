import socket
import threading

def handle_client(conn, addr):
    print(f"Connected by {addr}")
    conn.sendall(b"220 NixPerf Mock SMTP Server\r\n")
    while True:
        data = conn.recv(1024)
        if not data:
            break
        print(f"Received: {data.decode('utf-8', errors='replace').strip()}")
        if data.startswith(b"HELO") or data.startswith(b"EHLO"):
            conn.sendall(b"250 Hello\r\n")
        elif data.startswith(b"STARTTLS"):
            conn.sendall(b"220 Ready to start TLS\r\n")
            # Note: We don't actually do TLS here for simplicity, 
            # but we can see the client trying.
            break 
        elif data.startswith(b"MAIL FROM"):
            conn.sendall(b"250 OK\r\n")
        elif data.startswith(b"RCPT TO"):
            conn.sendall(b"250 OK\r\n")
        elif data.startswith(b"DATA"):
            conn.sendall(b"354 Start mail input; end with <CRLF>.<CRLF>\r\n")
        elif data.startswith(b"QUIT"):
            conn.sendall(b"221 Goodbye\r\n")
            break
        else:
            conn.sendall(b"250 OK\r\n")
    conn.close()

def start_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 1025))
        s.listen()
        print("Simple Mock SMTP server listening on 127.0.0.1:1025")
        while True:
            conn, addr = s.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr))
            t.start()

if __name__ == "__main__":
    start_server()

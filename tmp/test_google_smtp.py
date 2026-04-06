import smtplib
import ssl
from email.message import EmailMessage

def test_smtp_send():
    # --- CONFIGURATION ---
    # Your Gmail address
    SENDER_EMAIL = "your-email@gmail.com"
    # Your 16-character Google App Password (not your regular password)
    # Generate one at: https://myaccount.google.com/apppasswords
    APP_PASSWORD = "your-app-password"
    
    # Recipient email address (can be the same as sender for testing)
    RECIPIENT_EMAIL = "recipient@example.com"
    
    # SMTP details for Google
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 465  # Use 465 for SSL or 587 for STARTTLS
    
    # --- MESSAGE ---
    msg = EmailMessage()
    msg['Subject'] = "NixPerf Orchestrator - SMTP Test"
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECIPIENT_EMAIL
    msg.set_content("This is a test email from the NixPerf Orchestrator SMTP test script.\n\nIf you received this, your Google App Password and SMTP configuration are working correctly!")

    # --- SENDING ---
    print(f"Connecting to {SMTP_SERVER}:{SMTP_PORT}...")
    try:
        context = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
                print("Logging in...")
                server.login(SENDER_EMAIL, APP_PASSWORD)
                print("Sending email...")
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls(context=context)
                print("Logging in...")
                server.login(SENDER_EMAIL, APP_PASSWORD)
                print("Sending email...")
                server.send_message(msg)
                
        print("\nSUCCESS: Email sent successfully!")
        
    except Exception as e:
        print(f"\nFAILURE: Could not send email.")
        print(f"Error details: {e}")
        if "Authentication failed" in str(e):
            print("\nTIP: Make sure you're using a 16-character App Password, not your regular Google password.")
            print("Also, ensure '2-Step Verification' is enabled on your Google account.")

if __name__ == "__main__":
    test_smtp_send()

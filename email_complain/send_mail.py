from email.mime.text import MIMEText

from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import base64
import os

import pickle

SCOPES = ['https://www.googleapis.com/auth/gmail.send']

def get_service():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle','rb') as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pickle','wb') as f:
            pickle.dump(creds, f)

    return build('gmail', 'v1', credentials=creds)

def send_message(service, user_id, message):
    message = {'raw': base64.urlsafe_b64encode(message.as_bytes()).decode()}
    return service.users().messages().send(userId=user_id, body=message).execute()

service = get_service()
msg = MIMEText('Hello from API!')
msg['to'] = 'bgalitsky@hotmail.com'
msg['subject'] = 'Automated Message'
send_message(service, 'me', msg)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from typing import List, Optional
import os
from dotenv import load_dotenv
import re
import json
from datetime import datetime, timedelta
import pytz
import groq
from pydantic import BaseModel
from imap_tools import MailBox, AND
from email.utils import parseaddr
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Initialize FastAPI app
app = FastAPI(title="Email Extractor and Summarizer")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Supabase client
supabase: Client = create_client(
    os.getenv("SUPABASE_URL", ""),
    os.getenv("SUPABASE_KEY", "")
)

# Initialize Groq client
groq_client = groq.Groq(api_key=os.getenv("GROQ_API_KEY"))

# Create emails table if it doesn't exist
try:
    logger.info("Checking/Creating emails table in Supabase")
    # Check if table exists by attempting to select from it
    supabase.table("emails").select("*").limit(1).execute()
except Exception as e:
    logger.warning(f"Emails table might not exist: {str(e)}")
    logger.info("Please create the following table in Supabase:")
    logger.info("""
    create table if not exists emails (
        id bigint generated by default as identity primary key,
        subject text,
        from_address text,
        to_address text,
        date timestamp with time zone,
        text text,
        summary text,
        extracted_at timestamp with time zone default timezone('utc'::text, now())
    );
    """)

class EmailRequest(BaseModel):
    email_address: str
    password: str
    sender_addresses: List[str]

def summarize_single_email(email_content: dict) -> str:
    """Summarize a single email using Groq."""
    try:
        email_text = (
            f"Subject: {email_content['subject']}\n"
            f"From: {email_content['from_address']}\n"
            f"Date: {email_content['date']}\n"
            f"Content: {email_content['text'][:2000]}..."  # Increased limit for better context
        )
        
        logger.info(f"Generating summary for email: {email_content['subject'][:30]}...")
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant that summarizes emails. Provide a concise summary of the key points and any important action items. Keep the summary under 200 words."
                },
                {
                    "role": "user",
                    "content": f"Please provide a brief summary of this email:\n{email_text}"
                }
            ],
            model="mixtral-8x7b-32768",
            temperature=0.5,
            max_tokens=200
        )
        
        return chat_completion.choices[0].message.content
    except Exception as e:
        logger.error(f"Failed to generate summary for email: {str(e)}")
        return "Failed to generate summary"

def extract_emails_from_inbox(email_address: str, password: str, sender_addresses: List[str]) -> List[dict]:
    """Extract emails from specified senders in the last 24 hours."""
    email_data = []
    
    try:
        logger.info(f"Attempting to connect to Gmail IMAP for: {email_address}")
        
        # Calculate the date 24 hours ago in UTC
        utc = pytz.UTC
        date_since = datetime.now(utc) - timedelta(days=1)
        
        # For Gmail, we'll use SSL connection
        with MailBox('imap.gmail.com').login(email_address, password, initial_folder='INBOX') as mailbox:
            logger.info("Successfully connected to Gmail")
            
            for sender in sender_addresses:
                logger.info(f"Fetching emails from sender: {sender}")
                # Search criteria: from specific sender and within last 24 hours
                criteria = AND(
                    from_=sender,
                    date_gte=date_since.date()
                )
                
                messages = mailbox.fetch(criteria=criteria)
                
                for msg in messages:
                    try:
                        # Convert message date to UTC for comparison
                        msg_date = msg.date.replace(tzinfo=utc)
                        logger.debug(f"Message date: {msg_date}, Comparing with: {date_since}")
                        
                        # Only process if the message is within the last 24 hours
                        if msg_date >= date_since:
                            email_content = {
                                "subject": msg.subject,
                                "from_address": msg.from_,
                                "to_address": ", ".join(msg.to),
                                "date": msg_date.isoformat(),
                                "text": msg.text or msg.html,  # Fallback to HTML if text is empty
                                "extracted_at": datetime.now(utc).isoformat()
                            }
                            
                            # Generate summary for this specific email
                            email_content["summary"] = summarize_single_email(email_content)
                            
                            email_data.append(email_content)
                            logger.info(f"Processed email: {msg.subject[:30]}...")
                        else:
                            logger.debug(f"Skipping email from {msg_date} as it's older than {date_since}")
                    except Exception as e:
                        logger.error(f"Error processing individual email: {str(e)}")
                        continue
        
        logger.info(f"Successfully extracted {len(email_data)} emails")
        return email_data
    
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed to fetch emails: {error_msg}")
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Failed to fetch emails",
                "error": error_msg,
                "email": email_address
            }
        )

@app.post("/extract")
async def extract_from_email(request: EmailRequest):
    """Extract emails from specified senders in the last 24 hours and store them with summaries."""
    try:
        logger.info(f"Starting email extraction for: {request.email_address}")
        # Extract emails from the inbox
        extracted_emails = extract_emails_from_inbox(
            request.email_address,
            request.password,
            request.sender_addresses
        )
        
        if not extracted_emails:
            logger.warning("No emails found in the last 24 hours from specified senders")
            return {
                "message": "No emails found in the last 24 hours from specified senders",
                "senders_processed": request.sender_addresses
            }
        
        # Store in Supabase
        logger.info("Storing emails in Supabase")
        stored_count = 0
        for email_data in extracted_emails:
            try:
                response = supabase.table("emails").insert(email_data).execute()
                stored_count += 1
                logger.info(f"Stored email: {email_data['subject'][:30]}...")
            except Exception as e:
                logger.error(f"Failed to store email: {str(e)}")
                continue
        
        return {
            "message": "Emails extracted and stored successfully",
            "emails_found": len(extracted_emails),
            "emails_stored": stored_count,
            "senders_processed": request.sender_addresses
        }
    
    except Exception as e:
        logger.error(f"Error in extract_from_email: {str(e)}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/emails")
async def get_emails():
    """Retrieve all stored emails with their summaries."""
    try:
        logger.info("Retrieving emails from Supabase")
        response = supabase.table("emails").select("*").order('date.desc').execute()
        emails = response.data
        
        if not emails:
            logger.warning("No emails found in database")
            return {"message": "No emails found in database"}
        
        return {
            "emails": emails,
            "total_count": len(emails)
        }
    
    except Exception as e:
        logger.error(f"Error in get_emails: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

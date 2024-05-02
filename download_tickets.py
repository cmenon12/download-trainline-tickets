"""Script to download Trainline tickets from an email inbox."""

from __future__ import annotations

__author__ = "Christopher Menon"
__credits__ = "Christopher Menon"
__license__ = "gpl-3.0"

import configparser
import email
import imaplib
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from email import encoders
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from pytz import timezone

# The name of the config file
CONFIG_FILENAME = "config.ini"

# The timezone to use throughout
TIMEZONE = timezone("Europe/London")

# The date format to use for email dates
EMAIL_DATE_FORMAT = "%a, %d %b %Y %H:%M:%S %z (UTC)"

# The string to use for the email ID
EMAIl_ID_STRING = "cmenon12-download-trainline-tickets"

# The filename to use for the log file
LOG_FILENAME = f"download-tickets-{datetime.now(tz=TIMEZONE).strftime('%Y-%m-%d-%H.%M.%S')}.txt"


def parse_message(message: Message) -> list[str]:
    """Parse the email message to extract the ticket URLs.

    :param message: the email message to parse
    :type message: email.message.Message
    :return: the ticket URLs
    :rtype: str
    """

    html = None
    if message.is_multipart():
        for part in message.get_payload():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode()
    else:
        if message.get_content_type() == "text/html":
            html = message.get_payload(decode=True).decode()

    if html:
        soup = BeautifulSoup(html, "html.parser")
        urls = [a["href"] for a in soup.find_all("a", href=True) if
                "download.thetrainline.com" in a["href"]]
        return urls

    return []


def fetch_tickets(urls: list[str]) -> list[MIMEBase]:
    """Fetch the tickets from the given URLs.

    :param urls: a list of URLs to fetch the tickets from
    :type urls: list[str]
    :return: a list of PDF tickets as MIMEBase objects
    :rtype: list[email.mime.base.MIMEBase]
    """

    tickets = []
    for url in urls:

        # Fetch the HTML with the JavaScript redirect
        LOGGER.debug("Fetching ticket from %s.", url)
        session = requests.Session()
        response = session.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        scripts = soup.find_all('script')
        all_js = [script.string for script in scripts if script.string is not None]

        # Prepare second request with the request ID
        pattern = r"var requestId = '(.*?)';"
        match = re.search(pattern, all_js[0])
        if not match:
            LOGGER.warning("Could not find the request ID in the JavaScript, skipping.")
            continue
        req_id = match.group(1)
        token = url.split("#")[1]
        session.cookies.set(f"token-{req_id}", token)
        LOGGER.debug("Set cookie token-%s=%s", req_id, token)

        # Download the ticket
        url = f"https://download.thetrainline.com/resource/{req_id}"
        LOGGER.debug("Downloading ticket PDF from %s.", url)
        response = session.get(url, timeout=10)
        response.raise_for_status()

        # Stop if this is not a PDF file
        # Some links are to add the ticket to Google/Apple Wallet
        if response.headers["Content-Type"] != "application/pdf":
            LOGGER.warning("The ticket is not a PDF file, skipping.")
            continue

        # Save the ticket to a MIMEBase object
        pdf = MIMEBase("application", "pdf")
        pdf.set_payload(response.content)
        encoders.encode_base64(pdf)
        pdf.add_header("Content-Disposition",
                       response.headers["Content-Disposition"])
        filename = response.headers["Content-Disposition"].split("filename=")[1]
        pdf.add_header("Content-Description", filename)
        tickets.append(pdf)
        LOGGER.info("Downloaded ticket %s.", filename)

    return tickets


def check_if_already_processed(server: imaplib.IMAP4_SSL, message: Message) -> bool:
    """Check if the email has already been processed.

    :param server: the IMAP server
    :type server: imaplib.IMAP4_SSL
    :param message: the email message
    :type message: email.message.Message
    :return: False if the email has not already been processed, otherwise True
    :rtype: bool
    """

    status, items = server.search(None, f'(HEADER Subject "Re {message["Subject"]}")')
    if status != "OK":
        LOGGER.error("Could not search for emails.")
        return True
    items = items[0].split()

    # Get each email
    for email_id in items:
        _, data = server.fetch(email_id, "(RFC822)")
        email_message = email.message_from_bytes(data[0][1])

        # If the message ID contains the ID from this script, it has been processed
        if EMAIl_ID_STRING in email_message["Message-ID"]:
            return True

    return False


def prepare_ticket_email(tickets: list[MIMEBase], message: Message,
                         email_config: configparser.SectionProxy) -> MIMEMultipart:
    """Prepare an email with the tickets.

    :param tickets: the tickets to save
    :type tickets: list[email.mime.base.MIMEBase]
    :param message: the original email message
    :type message: email.message.Message
    :param email_config: the email config
    :type email_config: configparser.SectionProxy
    :return: the email with the tickets attached
    :rtype: email.mime.multipart.MIMEMultipart
    """

    # Create the message
    ticket_email = MIMEMultipart("alternative")
    ticket_email["Subject"] = f"Re: {message['Subject']}"
    ticket_email["To"] = message["To"]
    ticket_email["From"] = email_config["from"]
    date = (datetime.strptime(message["Date"], EMAIL_DATE_FORMAT) + timedelta(minutes=10)).strftime(
        EMAIL_DATE_FORMAT)
    LOGGER.debug("Setting the date to %s.", date)
    ticket_email["Date"] = date
    email_id = email.utils.make_msgid(idstring=EMAIl_ID_STRING, domain=email_config["imap_host"])
    LOGGER.debug("Email ID is %s.", email_id)
    ticket_email["Message-ID"] = email_id
    ticket_email["In-Reply-To"] = message["Message-ID"]
    ticket_email["References"] = message["Message-ID"]

    # Attach the tickets
    for ticket in tickets:
        ticket_email.attach(ticket)

    return ticket_email


def main():
    """The main function to run the script."""

    # Check that the config file exists
    try:
        open(CONFIG_FILENAME)  # pylint: disable=unspecified-encoding
        LOGGER.info("Loaded config %s.", CONFIG_FILENAME)
    except FileNotFoundError as error:
        print("The config file doesn't exist!")
        LOGGER.info("Could not find config %s, exiting.", CONFIG_FILENAME)
        time.sleep(5)
        raise FileNotFoundError("The config file doesn't exist!") from error

    # Fetch info from the config
    parser = configparser.ConfigParser()
    parser.read(CONFIG_FILENAME)
    email_config: configparser.SectionProxy = parser["email"]

    # Connect to IMAP server using IMAP4
    with imaplib.IMAP4_SSL(email_config["imap_host"],
                           int(email_config["imap_port"])) as server:
        server.login(email_config["username"], email_config["password"])
        server.select("inbox", readonly=True)
        LOGGER.debug("Connected to the IMAP server.")

        # Search for the emails
        status, items = server.search(None,
                                      '(FROM "auto-confirm@info.thetrainline.com" SUBJECT "Your '
                                      'eticket" SINCE 01-Jan-2024)')
        if status != "OK":
            LOGGER.error("Could not search for emails.")
            return
        LOGGER.debug("Found %s emails.", len(items[0].split()))
        for num in items[0].split():
            LOGGER.debug("Fetching email %s.", num)
            status, data = server.fetch(num, "(RFC822)")
            if status != "OK":
                LOGGER.error("Could not fetch the email.")
                continue
            message = email.message_from_bytes(data[0][1])
            LOGGER.info("Fetched email %s.", message["Subject"])

            # Check if the email has already been processed
            if check_if_already_processed(server, message):
                LOGGER.info("Email has already been processed, skipping.")
                continue

            # Check if the email is a ticket email
            urls = parse_message(message)
            LOGGER.debug("Found %s URLs.", len(urls))
            if len(urls) == 0:
                LOGGER.debug("Email does not contain any ticket URLs, skipping.")
                continue
            tickets = fetch_tickets(urls)
            LOGGER.debug("Found %s tickets.", len(tickets))
            if len(tickets) == 0:
                LOGGER.debug("Could not fetch any tickets, skipping.")
                continue
            ticket_email = prepare_ticket_email(tickets, message, email_config)
            status = server.append("inbox", "\\Seen",
                                   datetime.strptime(message["Date"],
                                                     EMAIL_DATE_FORMAT) + timedelta(
                                       minutes=10), ticket_email.as_bytes())
            if status[0] == "OK":
                LOGGER.info("Successfully saved the email with the %s tickets.", len(tickets))
            else:
                LOGGER.error("Could not save the email with the tickets.")
        LOGGER.info("Finished processing %s emails.", len(items[0].split()))
        server.close()
        server.logout()
        LOGGER.debug("Logged out of the IMAP server.")


if __name__ == "__main__":

    # Prepare the log
    Path("./logs").mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)5s in %(funcName)s() on line %(lineno)-3d | %(message)s"
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    file_handler = logging.FileHandler(
        f"./logs/{LOG_FILENAME}",
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    LOGGER = logging.getLogger()
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.addHandler(console_handler)
    LOGGER.addHandler(file_handler)
    LOGGER = logging.getLogger(__name__)

    # Run it
    main()

else:
    LOGGER = logging.getLogger(__name__)

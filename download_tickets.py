"""Script to download Trainline tickets from an email inbox."""

from __future__ import annotations

__author__ = "Christopher Menon"
__credits__ = "Christopher Menon"
__license__ = "gpl-3.0"

import configparser
import email
import imaplib
import logging
import pickle
import re
import sys
import time
from argparse import ArgumentParser
from datetime import datetime, timedelta
from email import encoders
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from pytz import timezone
from timelength import TimeLength

# The name of the config file
CONFIG_FILENAME = "config.ini"

# The timezone to use throughout
TIMEZONE = timezone("Europe/London")

COMPLETED_IDS_FILE = "completed_ids.pickle"

# The date format to use for email dates
EMAIL_DATE_FORMAT = "%a, %d %b %Y %H:%M:%S %z (UTC)"

# The string to use for the email ID
EMAIl_ID_STRING = "cmenon12-download-trainline-tickets"

# The filename to use for the log file
LOG_FILENAME = f"download-tickets-{datetime.now(tz=TIMEZONE).strftime('%Y-%m-%d-%H.%M.%S')}.txt"


def parse_args() -> dict[str, Any]:
    """Parse the command-line arguments.

    :return: the parsed arguments
    :rtype: dict[str, Any]
    """

    # Get the arguments
    parser = ArgumentParser()
    parser.add_argument("-a", "--age", required=True,
                        help="Max age of emails in human-readable format, e.g. \"1 day\" or \"6 "
                             "hours\"")
    args = parser.parse_args()

    # Parse the age
    age = TimeLength(args.age).result
    if age.success:
        age = age.seconds
    else:
        LOGGER.error("Could not parse the age %s, exiting.", args.age)
        sys.exit(1)

    return {"age": age}


def get_completed_ids() -> set[str]:
    """Get the set of completed message IDs.

    :return: the set of completed message IDs
    :rtype: set[str]
    """

    if Path(COMPLETED_IDS_FILE).exists():
        completed_ids: set[str] = pickle.load(open(COMPLETED_IDS_FILE, "rb"))
        if not isinstance(completed_ids, set):
            completed_ids = set()
            LOGGER.warning("The completed IDs file is not a set, starting fresh.")
        else:
            LOGGER.info("Loaded %s completed message IDs.", len(completed_ids))
    else:
        completed_ids = set()
        LOGGER.info("No completed IDs file found, starting fresh.")

    return completed_ids


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


def check_if_already_processed(server: imaplib.IMAP4_SSL, message: Message,
                               completed_ids: set[str]) -> bool:
    """Check if the email has already been processed.

    :param server: the IMAP server
    :type server: imaplib.IMAP4_SSL
    :param message: the email message
    :type message: email.message.Message
    :param completed_ids: the set of completed message IDs
    :type completed_ids: set[str]
    :return: False if the email has not already been processed, otherwise True
    :rtype: bool
    """

    # Check if the email ID has been marked as completed
    if message["Message-ID"] in completed_ids:
        LOGGER.info("Email has already been processed (saved ID), skipping.")
        return True

    status, items = server.search(None, f'(HEADER Subject "Re {message["Subject"]}")')
    if status != "OK":
        LOGGER.error("Could not search for emails, skipping.")
        return True
    items = items[0].split()

    # Get each email
    for email_id in items:
        _, data = server.fetch(email_id, "(RFC822)")
        email_message = email.message_from_bytes(data[0][1])

        # If the message ID contains the ID from this script, it has been processed
        if EMAIl_ID_STRING in email_message["Message-ID"]:
            LOGGER.info("Email has already been processed (found in inbox), skipping.")
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

    # Parse the args
    args = parse_args()
    LOGGER.info("Parsed the arguments: %s.", args)

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

    # Get the previously completed message IDs
    completed_ids = get_completed_ids()

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
            if check_if_already_processed(server, message, completed_ids):
                completed_ids.add(message["Message-ID"])
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
                completed_ids.add(message["Message-ID"])
                LOGGER.debug("Saving the message ID %s to the completed_ids.",
                             message["Message-ID"])
            else:
                LOGGER.error("Could not save the email with the tickets.")
        LOGGER.info("Finished processing %s emails.", len(items[0].split()))
        pickle.dump(completed_ids, open(COMPLETED_IDS_FILE, "wb"))
        LOGGER.info("Saved %s completed message IDs.", len(completed_ids))
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

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
import time
from datetime import datetime
from email import encoders
from email.message import Message
from email.mime.base import MIMEBase
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from pytz import timezone

# The name of the config file
CONFIG_FILENAME = "config.ini"

# The timezone to use throughout
TIMEZONE = timezone("Europe/London")

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


def fetch_ticket(url: str) -> MIMEBase | None:
    """Fetch the ticket from the given URL.

    :param url: the URL to fetch the ticket from
    :type url: str
    :return: the PDF ticket as a MIMEBase object
    :rtype: email.mime.base.MIMEBase | None
    """

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
        return None
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
        return None

    # Save the ticket to a MIMEBase object
    pdf = MIMEBase("application", "pdf")
    pdf.set_payload(response.content)
    encoders.encode_base64(pdf)
    pdf.add_header("Content-Disposition",
                   response.headers["Content-Disposition"])
    filename = response.headers["Content-Disposition"].split("filename=")[1]
    pdf.add_header("Content-Description", filename)
    LOGGER.debug("Downloaded ticket %s.", filename)

    return pdf


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
        _, data = server.search(None, '(FROM "auto-confirm@info.thetrainline.com" SUBJECT "Your '
                                      'eticket" SINCE 01-Jan-2024)')
        LOGGER.debug("Found %s emails.", len(data[0].split()))
        for num in data[0].split():
            LOGGER.debug("Processing email %s.", num)
            _, data = server.fetch(num, "(RFC822)")
            message = email.message_from_bytes(data[0][1])
            urls = parse_message(message)
            LOGGER.debug("Found %s URLs.", len(urls))
            for url in urls:
                fetch_ticket(url)
        server.close()
        server.logout()


if __name__ == "__main__":

    # Prepare the log
    Path("./logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        format="%(asctime)s | %(levelname)5s in %(module)s.%(funcName)s() on line %(lineno)-3d | "
               "%(message)s",
        level=logging.DEBUG,
        handlers=[
            logging.FileHandler(
                f"./logs/{LOG_FILENAME}",
                mode="a",
                encoding="utf-8")])
    LOGGER = logging.getLogger(__name__)

    # Run it
    main()

else:
    LOGGER = logging.getLogger(__name__)

"""Script to download Trainline tickets from an email inbox."""

__author__ = "Christopher Menon"
__credits__ = "Christopher Menon"
__license__ = "gpl-3.0"

import configparser
import email
import imaplib
import logging
import time
from datetime import datetime
from pathlib import Path

from pytz import timezone

# The name of the config file
CONFIG_FILENAME = "config.ini"

# The timezone to use throughout
TIMEZONE = timezone("Europe/London")

# The filename to use for the log file
LOG_FILENAME = f"download-tickets-{datetime.now(tz=TIMEZONE).strftime('%Y-%m-%d-%H.%M.%S')}.txt"


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

        # Search for the emails
        _, data = server.search(None, '(FROM "auto-confirm@info.thetrainline.com" SUBJECT "Your '
                                      'eticket" SINCE 01-Jan-2024)')
        for num in data[0].split():
            _, data = server.fetch(num, "(RFC822)")
            message = email.message_from_bytes(data[0][1])
            print(message)
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

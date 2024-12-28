#!/usr/bin/env python3
import time
import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import NoSuchElementException, TimeoutException
import logging
import os
import urllib.parse
import re

logging.basicConfig(filename='scraper.log', level=logging.INFO)

# ------------------------ Configuration ------------------------

# Path to the Chrome binary
CHROME_BINARY_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Path to the ChromeDriver executable
# Update this path based on where ChromeDriver is installed on your system
# Common paths:
# - /usr/local/bin/chromedriver (Intel Macs)
# - /opt/homebrew/bin/chromedriver (Apple Silicon Macs)
CHROMEDRIVER_PATH = "/opt/homebrew/bin/chromedriver"

# Institution and Program
INSTITUTION = "Yale University"
PROGRAM = "Political Science"
DEGREE = "PhD"
SEASON = ""  # Leave empty or specify if needed

# URL Parameters
SORT = "newest"

# Pages to scrape
START_PAGE = 1
END_PAGE = 170  # Adjust based on the number of pages you want to scrape

# Output CSV file
OUTPUT_CSV = "yale_polisci_recent.csv"

# Log file
LOG_FILE = "fetch_data.log"

# Delay configurations
PAGE_LOAD_DELAY = 5  # Seconds to wait for the page to load
REQUEST_DELAY = 2    # Seconds to wait between page requests

# ---------------------------------------------------------------

def setup_logging():
    """
    Sets up the logging configuration.
    """
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format='%(asctime)s %(levelname)s:%(message)s'
    )
    logging.info("Logging is set up.")

def setup_driver():
    """
    Sets up the Selenium WebDriver with specified options and paths.
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless")  # Run Chrome in headless mode
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--user-agent=GradStatDataCollector/1.0 (+https://yourdomain.com/contact)")
    
    # Explicitly set the path to the Chrome binary
    chrome_options.binary_location = CHROME_BINARY_PATH

    # Verify Chrome binary exists
    if not os.path.exists(CHROME_BINARY_PATH):
        logging.critical(f"Chrome binary not found at {CHROME_BINARY_PATH}. Please verify the path.")
        raise FileNotFoundError(f"Chrome binary not found at {CHROME_BINARY_PATH}.")

    # Verify ChromeDriver exists
    if not os.path.exists(CHROMEDRIVER_PATH):
        logging.critical(f"ChromeDriver not found at {CHROMEDRIVER_PATH}. Please install ChromeDriver correctly.")
        raise FileNotFoundError(f"ChromeDriver not found at {CHROMEDRIVER_PATH}.")

    # Initialize WebDriver
    service = Service(CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=chrome_options)
    logging.info("WebDriver has been initialized.")
    return driver

def construct_url(page_num):
    """
    Constructs the GradCafe survey URL based on the page number and filters.
    
    Args:
        page_num (int): The page number to construct the URL for.
    
    Returns:
        str: The constructed URL.
    """
    base_url = "https://www.thegradcafe.com/survey/"
    query_params = {
        "q": "",  # Empty as per the desired URL
        "sort": SORT,
        "institution": INSTITUTION,
        "program": PROGRAM,
        "degree": DEGREE,
        "season": SEASON,
        "page": page_num
    }
    # Encode the query parameters properly
    encoded_params = urllib.parse.urlencode(query_params)
    full_url = f"{base_url}?{encoded_params}"
    return full_url

def parse_stats(stats_raw):
    """
    Parses the raw stats string to extract GPA and GRE scores.
    
    Args:
        stats_raw (str): The raw stats string from the table.
    
    Returns:
        dict: A dictionary containing GPA and GRE scores.
    """
    gpa = None
    gre_verbal = None
    gre_quant = None
    gre_writing = None

    # Example stats_raw format: "GPA: 3.8, GRE Verbal: 160, GRE Quant: 165, GRE AWA: 4.5"
    # Adjust the regex patterns based on the actual format observed

    # Extract GPA
    gpa_match = re.search(r'GPA[:\s]+(\d\.\d+)', stats_raw, re.IGNORECASE)
    if gpa_match:
        gpa = float(gpa_match.group(1))

    # Extract GRE Verbal
    gre_v_match = re.search(r'GRE Verbal[:\s]+(\d+)', stats_raw, re.IGNORECASE)
    if gre_v_match:
        gre_verbal = int(gre_v_match.group(1))

    # Extract GRE Quantitative
    gre_q_match = re.search(r'GRE Quantitative[:\s]+(\d+)', stats_raw, re.IGNORECASE)
    if gre_q_match:
        gre_quant = int(gre_q_match.group(1))

    # Extract GRE Analytical Writing
    gre_w_match = re.search(r'GRE Analytical Writing[:\s]+(\d\.\d+)', stats_raw, re.IGNORECASE)
    if gre_w_match:
        gre_writing = float(gre_w_match.group(1))

    return {
        'gpa': gpa,
        'gre_verbal': gre_verbal,
        'gre_quant': gre_quant,
        'gre_writing': gre_writing
    }

def scrape_gradcafe_page(driver, page_num):
    """
    Scrape a single page of Yale Political Science PhD results.

    Args:
        driver: Selenium WebDriver instance.
        page_num (int): The page number to scrape.

    Returns:
        list: A list of dictionaries containing scraped data.
    """
    url = construct_url(page_num)
    logging.info(f"Fetching page {page_num}: {url}")
    try:
        driver.get(url)
        time.sleep(PAGE_LOAD_DELAY)  # Wait for JavaScript to load content

        entries = []

        # Locate the table by class name; inspect the website to find the correct class
        try:
            table = driver.find_element(By.CLASS_NAME, 'submission-table')  # Update class name if different
            rows = table.find_elements(By.TAG_NAME, 'tr')[1:]  # Skip header row

            for row in rows:
                cells = row.find_elements(By.TAG_NAME, 'td')
                if len(cells) < 7:  # Ensure there are enough cells
                    continue  # Incomplete row

                # Extract data fields
                institution = cells[0].text.strip()
                program = cells[1].text.strip()
                degree = cells[2].text.strip()
                season = cells[3].text.strip()
                decision = cells[4].text.strip()
                # Assuming 'stats' is in cell index 6 (7th column)
                stats_raw = cells[6].text.strip() if len(cells) > 6 else ''

                # Only include Yale Political Science PhD entries
                if ('Yale' in institution and 
                    'Political Science' in program and 
                    'PhD' in degree):
                    
                    # Parse GPA and GRE scores
                    stats = parse_stats(stats_raw)

                    # Add the row to our results
                    entries.append({
                        'institution': institution,
                        'program': program,
                        'degree': degree,
                        'season': season,
                        'decision': decision,
                        'gpa': stats['gpa'],
                        'gre_verbal': stats['gre_verbal'],
                        'gre_quant': stats['gre_quant'],
                        'gre_writing': stats['gre_writing'],
                        'stats_raw': stats_raw
                    })

            logging.info(f"Page {page_num}: Fetched {len(entries)} entries.")
            return entries

        except NoSuchElementException:
            logging.warning(f"No table found on page {page_num}.")
            # Save the page source for debugging
            page_source_file = f"page_source_political_science_page{page_num}.html"
            with open(page_source_file, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            logging.info(f"Saved page source to {page_source_file} for manual inspection.")
            return []

    except Exception as e:
        logging.error(f"Error scraping page {page_num}: {str(e)}")
        # Optionally, save the page source for debugging
        page_source_file = f"error_page_source_political_science_page{page_num}.html"
        with open(page_source_file, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        logging.info(f"Saved page source to {page_source_file} due to error.")
        return []

def scrape_all_pages(driver, start_page=START_PAGE, end_page=END_PAGE):
    """
    Scrape multiple pages of results.

    Args:
        driver: Selenium WebDriver instance.
        start_page (int): The starting page number.
        end_page (int): The ending page number.

    Returns:
        pd.DataFrame: A DataFrame containing all scraped data.
    """
    all_results = []
    for page in range(start_page, end_page + 1):
        logging.info(f"Starting data fetch for page {page}.")
        results = scrape_gradcafe_page(driver, page)
        if not results:
            logging.info(f"No results found on page {page}. Assuming no more pages to scrape.")
            break  # Exit the loop if no data is found
        all_results.extend(results)
        logging.info(f"Completed data fetch for page {page}. Total entries collected so far: {len(all_results)}.")
        time.sleep(REQUEST_DELAY)  # Respectful delay between requests

    return pd.DataFrame(all_results)

def process_data(df):
    """
    Process and clean the scraped data.

    Args:
        df (pd.DataFrame): The DataFrame to process.

    Returns:
        pd.DataFrame: The cleaned DataFrame.
    """
    if df is None or len(df) == 0:
        logging.warning("No data to process.")
        return None

    # Clean up decisions
    decision_mapping = {
        'Accepted': 'Accept',
        'Rejected': 'Reject',
        'Wait Listed': 'Waitlist',
        'Interview': 'Other',
        'Other': 'Other'
    }
    df['decision'] = df['decision'].map(decision_mapping).fillna('Unknown')

    # Handle missing GPA and GRE scores
    df['gpa'] = pd.to_numeric(df['gpa'], errors='coerce')
    df['gre_verbal'] = pd.to_numeric(df['gre_verbal'], errors='coerce')
    df['gre_quant'] = pd.to_numeric(df['gre_quant'], errors='coerce')
    df['gre_writing'] = pd.to_numeric(df['gre_writing'], errors='coerce')

    # Fill missing GPA with mean GPA
    mean_gpa = df['gpa'].mean()
    df['gpa'].fillna(mean_gpa, inplace=True)

    # Fill missing GRE scores with median
    median_gre_verbal = df['gre_verbal'].median()
    median_gre_quant = df['gre_quant'].median()
    median_gre_writing = df['gre_writing'].median()

    df['gre_verbal'].fillna(median_gre_verbal, inplace=True)
    df['gre_quant'].fillna(median_gre_quant, inplace=True)
    df['gre_writing'].fillna(median_gre_writing, inplace=True)

    return df

def main():
    """
    Main function to orchestrate data scraping and processing.
    """
    setup_logging()
    logging.info("Starting GradCafe data collection script for Yale University Political Science PhD program.")

    # Initialize WebDriver
    try:
        driver = setup_driver()
    except FileNotFoundError as e:
        logging.critical(f"Setup failed: {e}")
        return
    except Exception as e:
        logging.critical(f"Unexpected error during WebDriver setup: {e}")
        return

    # Scrape data
    df = scrape_all_pages(driver)

    if df is not None and len(df) > 0:
        # Process data
        df = process_data(df)

        # Save results
        try:
            df.to_csv(OUTPUT_CSV, index=False)
            logging.info(f"Data successfully saved to {OUTPUT_CSV}. Total entries: {len(df)}.")
            print(f"\nData saved to {OUTPUT_CSV}. Total entries: {len(df)}.")
        except Exception as e:
            logging.error(f"Failed to save data to CSV. Error: {e}")
            print(f"Failed to save data to CSV. Error: {e}")

        # Print summary
        print("\nDecision Summary:")
        print(df['decision'].value_counts())

        # Print unique institutions (for verification)
        print("\nUnique institutions found:")
        print(df['institution'].unique())

    else:
        print("No data scraped.")
        logging.info("No data scraped.")

    # Close WebDriver
    driver.quit()
    logging.info("WebDriver has been closed.")
    logging.info("GradCafe data collection script has completed.")

if __name__ == "__main__":
    main()
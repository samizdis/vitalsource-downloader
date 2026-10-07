import argparse
import re
import os
import shutil
import time
from tqdm import tqdm
from modules.browser import BrowserManager
from modules.navigator import Navigator
from modules.capturer import Capturer
from modules.ocr import OCRManager
from modules.pdf_maker import PDFMaker

BOOKSHELF_HOME = "https://bookshelf.vitalsource.com/"
READER_URL_RE = re.compile(r"https://bookshelf\.vitalsource\.com/reader/books/(\d{9,13}X?)")


def resolve_book_url(book):
    """Accepts an ISBN or a reader URL; returns a reader URL, or None to open the library."""
    if not book:
        return None
    book = book.strip()
    if re.fullmatch(r"\d{9,13}X?", book):
        return f"https://bookshelf.vitalsource.com/reader/books/{book}"
    return book


def isbn_from_url(url):
    match = READER_URL_RE.match(url or "")
    return match.group(1) if match else None


def main():
    parser = argparse.ArgumentParser(
        description="VitalSource to PDF Converter",
        epilog="With no arguments, the Bookshelf library opens so you can log in and open a book.")
    parser.add_argument("book", nargs="?", help="ISBN or reader URL of the book (optional)")
    parser.add_argument("--url", help="Reader URL of the book (same as the positional argument)")
    parser.add_argument("--output", help="Output PDF filename (defaults to <ISBN>.pdf)")
    parser.add_argument("--pages", type=str, default="all", help="Pages to capture (e.g., '1-10', '1,3,5', 'all')")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode (needs a saved cookies.json)")
    parser.add_argument("--pdf-width", type=float, default=None,
                        help="PDF page width in inches (default: guessed from the page proportions)")
    args = parser.parse_args()

    book_url = resolve_book_url(args.url or args.book)
    output_filename = args.output

    # Initialize Modules
    browser = BrowserManager(headless=args.headless)
    output_dir = "output/temp_pages"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    try:
        browser.start()
        if book_url:
            browser.navigate_to_book(book_url)
            print("Please log in if necessary. Press Enter in the terminal to continue once the book is fully loaded.")
        else:
            browser.page.goto(BOOKSHELF_HOME)
            print("Log in if necessary, open the book you want to download, then press Enter in the terminal.")
        input()

        # Use whatever book is open in the browser (the user may have navigated)
        current = browser.page.evaluate("location.href")
        isbn = isbn_from_url(current)
        if not isbn:
            print(f"No book is open in the reader (current page: {current}). "
                  "Open a book at bookshelf.vitalsource.com/reader/books/<ISBN> and try again.")
            return
        book_url = f"https://bookshelf.vitalsource.com/reader/books/{isbn}"
        output_filename = output_filename or f"{isbn}.pdf"
        if not output_filename.endswith(".pdf"):
            output_filename += ".pdf"

        # Save session and switch to high-res mode
        browser.save_cookies("cookies.json")
        browser.set_high_res_viewport()

        # Core components
        navigator = Navigator(browser.page)
        capturer = Capturer(browser.page, output_dir)
        
        metadata = navigator.extract_metadata()
        print(f"Book Metadata: {metadata}")

        # Page width: explicit flag, else guessed from the page proportions
        if args.pdf_width:
            target_width = args.pdf_width
            print(f"Using page width from --pdf-width: {target_width} inches")
        else:
            guess = navigator.guess_page_size()
            if guess:
                target_width = guess["width"]
                print(f"Page proportions {guess['ratio']:.3f} look like {guess['name']} "
                      f"({guess['width']} x {guess['height']} in). Override with --pdf-width if wrong.")
            else:
                target_width = 9.15
                print(f"Could not measure the page; using default width {target_width} inches.")

        ocr = OCRManager(target_width_inches=target_width)
        navigator.extract_toc()

        # Determine page range from --pages (e.g. "1-50", "10", "3,7-9"; "all" = everything)
        page_files = []
        max_pages_limit = 2500
        start_page = 1
        end_page = None
        if args.pages and args.pages.lower() != "all":
            try:
                indices = set()
                for part in args.pages.split(','):
                    part = part.strip()
                    if '-' in part:
                        lo, hi = map(int, part.split('-'))
                        indices.update(range(lo, hi + 1))
                    elif part:
                        indices.add(int(part))
                if indices:
                    start_page = max(1, min(indices))
                    end_page = max(indices)
            except Exception:
                print("Error parsing pages argument. Capturing all.")
                start_page, end_page = 1, None

        # Always jump explicitly: the reader otherwise resumes at the last-read position,
        # not at page 1. Reader pageids are zero-based (pageid/0 is the first page).
        print(f"Jumping to page {start_page}...")
        jump_url = f"{book_url}/pageid/{start_page - 1}"
        browser.page.goto(jump_url)
        try:
            browser.page.wait_for_selector("iframe[src*='jigsaw'], #vst-app-container, div#print-content", timeout=20000)
            time.sleep(3)
        except Exception:
            print("Warning: Jump navigation timed out.")

        # Initialize counters
        page_count = start_page - 1
        # Set limit
        if end_page:
            loop_limit = end_page
            pbar_total = loop_limit - page_count
            print(f"Starting capture from page {start_page} to {loop_limit}")
        else:
            # Page count comes from the reader's page list when available; otherwise the
            # loop simply ends when the Next button stops advancing the reader.
            detected_total = navigator.get_total_pages(isbn)
            loop_limit = detected_total if detected_total else max_pages_limit
            pbar_total = (detected_total - page_count) if detected_total else None
            print(f"Starting capture from page {start_page} until the end of the book"
                  + (f" ({detected_total} pages)" if detected_total else ""))

        pbar = tqdm(total=pbar_total, desc="Capturing Pages", unit="page")

        # Page capture loop
        while page_count < loop_limit:
            try:
                page_count += 1
                
                # Capture and process page
                capturer.capture_page(page_count)
                
                filename = f"page_{page_count:04d}.png"
                filepath = os.path.join(output_dir, filename)
                
                optimized_pdf_page = ocr.image_to_pdf(filepath)
                if optimized_pdf_page:
                    page_files.append(optimized_pdf_page)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                
                pbar.update(1)
                
                if not navigator.next_page():
                    break
                
                if page_count % 25 == 0:
                    try:
                        pbar.set_description("Optimizing RAM...")
                        import gc
                        gc.collect()
                        browser.page.reload(timeout=60000)
                        browser.page.wait_for_load_state("domcontentloaded", timeout=60000)
                        browser.set_high_res_viewport()
                        capturer = Capturer(browser.page, output_dir)
                        navigator = Navigator(browser.page)
                    except Exception as e:
                        print(f"RAM Optimization warning: {e}")
                    finally:
                        pbar.set_description("Capturing Pages")

            except Exception as e:
                print(f"Error capturing page {page_count}: {e}")
                if not navigator.next_page():
                    break

        pbar.close()

        # Generate Final PDF
        if page_files:
            print(f"Generating PDF: {output_filename}...")
            # Save final PDF in the 'output' folder
            pdf_maker = PDFMaker("output")
            
            pdf_maker.make_pdf(page_files, navigator.toc, {}, output_filename, metadata)
            print(f"Successfully created output/{output_filename}")
            
            # Only delete the temporary image subdirectory
            shutil.rmtree(output_dir)
            print("Temporary image files cleaned up.")
        else:
            print("No pages captured.")

    except KeyboardInterrupt:
        print("\nProcess interrupted by user.")
    except Exception as e:
        print(f"\nAn error occurred: {e}")
    finally:
        print("Cleaning up...")
        browser.close()
        # Optional: Clean up temp files in output/temp_pages

if __name__ == "__main__":
    main()

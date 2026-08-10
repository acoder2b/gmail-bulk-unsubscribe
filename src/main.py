import re
import sys
from collections import Counter
from pathlib import Path

try:
    from dotenv import load_dotenv

    # Explicit path, not auto-discovery: load_dotenv()'s default upward
    # search is unreliable depending on how/where this script is invoked
    # from, and the README's own instructions (cd into src/, run from
    # there) are exactly the case where it silently failed to find .env
    # in the project root during testing. Anchoring to this file's own
    # location means it works regardless of the caller's cwd.
    load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # optional — env vars can also be exported directly

from methods import GmailMethod
from report import build_report, format_report_text

# Default AI provider for the weekly clutter report. Override per-run at
# the prompt, or change this if you want a different default.
DEFAULT_AI_PROVIDER = "anthropic"


def main():
    """Main function to run the Gmail Cleaner."""
    gmail = GmailMethod()

    while True:
        print("""
1. Show the most common senders
2. Move messages from a specific sender to trash
3. Move messages from a specific sender to the spam folder
4. Move all messages from the spam to the trash folder
5. Move messages matching a specific label to trash
6. Add a label to emails matching a specified sender
7. Bulk-unsubscribe from one or more senders (List-Unsubscribe)
8. Generate weekly clutter report (AI-powered, suggests only)
9. Exit
        """)

        try:
            user_choice = int(input("Choose an option: "))

            if user_choice == 1:
                # Show the most common senders
                if not gmail.users:
                    [messages, _history] = gmail.list_messages("me")
                    gmail.batch_get(messages)

                sender_counts = Counter(gmail.users).most_common()
                num_senders = int(input("How many senders do you want to display? "))

                print("You have:")
                gmail.total_from_users = 0

                for sender, count in sender_counts[:num_senders]:
                    # Extract email from sender string
                    email_match = re.search(r"(?<=<)(.*)(?=>)", sender)
                    display_sender = email_match.group() if email_match else sender

                    gmail.total_from_users += count
                    print(f"- {count} e-mails from {display_sender}.")

                print(f"In total you have {gmail.total_messages} e-mails.")

            elif user_choice == 2:
                # Move messages from sender to trash (using batch processing)
                sender = input("Choose sender whose messages you want to delete: ")
                messages = gmail.list_messages_matching_query("me", f"from:{sender}")

                if not messages:
                    print(f"No messages found from {sender}")
                    continue

                # Reset counter and use batch deletion
                gmail.moved_to_trash = 0
                gmail.batch_delete(messages)

                print(
                    f"Process deleting e-mails from {sender} completed. "
                    f"All {gmail.moved_to_trash} e-mails have been moved to the trash."
                )

            elif user_choice == 3:
                # Move messages from sender to spam (using batch processing)
                sender = input(
                    "Choose sender whose messages you want to move into spam: "
                )
                messages = gmail.list_messages_matching_query("me", f"from:{sender}")

                if not messages:
                    print(f"No messages found from {sender}")
                    continue

                gmail.moved_to_spam = 0
                gmail.batch_spam(messages)

                print(f"Moved {gmail.moved_to_spam} e-mails to the spam folder.")

            elif user_choice == 4:
                # Move all spam to trash (using batch processing)
                messages = gmail.list_messages_matching_label("me", "SPAM")

                if not messages:
                    print("No messages found in spam")
                    continue

                # Reset counter and use batch deletion
                gmail.moved_to_trash = 0
                gmail.batch_delete(messages)

                print(
                    f"Emptied spam. All {gmail.moved_to_trash} e-mails have been moved to trash."
                )

            elif user_choice == 5:
                # Move messages with label to trash (using batch processing)
                if input("Do you know all your label IDs? (yes/no): ").lower() in [
                    "no",
                    "n",
                ]:
                    print("You have these labels:")
                    for label in gmail.list_labels("me"):
                        print(f"Label: {label['name']}; ID: {label['id']}")

                label_id = input(
                    "Messages matching what label ID do you want to delete? "
                )
                messages = gmail.list_messages_matching_label("me", label_id)

                if not messages:
                    print(f"No messages found with label ID {label_id}")
                    continue

                # Reset counter and use batch deletion
                gmail.moved_to_trash = 0
                gmail.batch_delete(messages)

                print(f"All {gmail.moved_to_trash} e-mails have been moved to trash.")

            elif user_choice == 6:
                # Add label to emails from sender
                label_name = input("What is the name of the label you want to attach? ")
                sender = input(
                    "Choose sender whose messages you want to attach this label to: "
                )

                # Check if label exists, create if not
                labels = gmail.list_labels("me")
                if not gmail.label_check(labels, label_name):
                    label_info = gmail.create_label("me", label_name)
                    label_id = label_info["id"]
                    print(f"Created new label: {label_name}")
                else:
                    label_id = next(
                        label["id"] for label in labels if label["name"] == label_name
                    )

                messages = gmail.list_messages_matching_query("me", f"from:{sender}")

                if not messages:
                    print(f"No messages found from {sender}")
                    continue

                # Reset counter and use batch labeling (already implemented in attach_label)
                gmail.labels = 0
                gmail.batch_label(messages, label_id, "me")
                print(f'Attached the label: "{label_name}" to {gmail.labels} e-mails.')

            elif user_choice == 7:
                # Bulk-unsubscribe using List-Unsubscribe / List-Unsubscribe-Post
                # headers (RFC 2369 / RFC 8058) — no browser required for
                # senders that support Gmail's own one-click unsubscribe.
                raw = input(
                    "Sender(s) to unsubscribe from (comma-separated for multiple): "
                )
                senders = [s.strip() for s in raw.split(",") if s.strip()]

                if not senders:
                    print("No senders entered.")
                    continue

                results = gmail.bulk_unsubscribe(senders)

                one_click = results.get("unsubscribed_one_click", [])
                manual_link = results.get("manual_link_only", [])
                mailto_only = results.get("mailto_only", [])
                not_found = results.get("not_found", [])
                no_header = results.get("no_header", [])
                failed = (
                    results.get("http_error", [])
                    + results.get("request_failed", [])
                    + results.get("fetch_failed", [])
                    + results.get("unknown_format", [])
                )

                print(f"\nDone. {len(one_click)}/{len(senders)} unsubscribed automatically (one-click).\n")

                if one_click:
                    print("Unsubscribed:")
                    for r in one_click:
                        print(f"  - {r['from']}")

                if manual_link:
                    print("\nNo one-click support — open these links to finish manually:")
                    for r in manual_link:
                        print(f"  - {r['from']}: {r['url']}")

                if mailto_only:
                    print("\nUnsubscribe requires sending an email (not done automatically):")
                    for r in mailto_only:
                        print(f"  - {r['from']}: {r['target']}")

                if no_header:
                    print("\nNo List-Unsubscribe header found (sender may not support it):")
                    for r in no_header:
                        print(f"  - {r['from']}")

                if not_found:
                    print("\nNo messages found from:")
                    for r in not_found:
                        print(f"  - {r['sender']}")

                if failed:
                    print("\nFailed / unexpected:")
                    for r in failed:
                        print(f"  - {r.get('from', r['sender'])}: {r.get('error', r.get('code', 'unknown error'))}")

            elif user_choice == 8:
                # Weekly clutter report — suggests only, never acts.
                provider_name = input(
                    f"AI provider to use [anthropic/openai] (default: {DEFAULT_AI_PROVIDER}): "
                ).strip() or DEFAULT_AI_PROVIDER

                limit_raw = input(
                    "How many top unread senders to analyze? (default: 60): "
                ).strip()
                limit = int(limit_raw) if limit_raw else 60

                days_raw = input(
                    "How many days back should count as \"this week\"? (default: 7): "
                ).strip()
                days = int(days_raw) if days_raw else 7

                try:
                    rows = build_report(
                        gmail, provider_name=provider_name, limit=limit, days=days
                    )
                except Exception as e:
                    print(f"Could not generate report: {e}")
                    continue

                report_text = format_report_text(rows)
                print("\n" + report_text + "\n")

                if input("Email this report to yourself? (yes/no): ").lower() in ["yes", "y"]:
                    try:
                        own_address = gmail.get_own_email_address()
                        gmail.send_email(
                            to=own_address,
                            subject="Weekly Gmail Clutter Report",
                            body=report_text,
                        )
                        print(f"Report emailed to {own_address}.")
                    except Exception as e:
                        print(f"Could not send email: {e}")

            elif user_choice == 9:
                # Exit
                print("Exiting Gmail Cleaner. Goodbye!")
                sys.exit(0)

            else:
                print("Invalid option! Please try again.")

        except ValueError:
            print("Invalid input! Please enter a number.")

        except KeyboardInterrupt:
            print("\nOperation cancelled by user.")

        except Exception as e:
            print(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    main()

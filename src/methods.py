import base64
import random
import re
import time
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

import requests
from googleapiclient.errors import HttpError
from tqdm import tqdm

from client import GmailClient
from whitelist import is_whitelisted, load_whitelist

# fmt: off
# https://developers.google.com/gmail/api/reference/quota#per-method_quota_usage
TRASH_BATCH_SIZE = 20   # messages.trash is 5 units
LIST_BATCH_SIZE = 20    # messages.list is 5 units
MODIFY_BATCH_SIZE = 20  # messages.modify is 5 units
DELETE_BATCH_SIZE = 10  # messages.delete is 10 units
SPAM_BATCH_SIZE = 20    # uses batchModify which is 5 units
LABEL_BATCH_SIZE = 20   # uses batchModify which is 5 units
GET_BATCH_SIZE = 20     # messages.get is 5 units
# fmt: on


class GmailMethod:
    """Provides methods for interacting with Gmail."""

    def __init__(self):
        self.gmailclient = GmailClient()
        self.users = []
        self.users_query = []
        self.total_from_users = 0
        self.messages = []
        self.total_messages = 0
        self.moved_to_trash = 0
        self.moved_to_spam = 0
        self.deleted = 0
        self.labels = 0

        # Loaded once at startup. Re-run reload_whitelist() if you edit
        # whitelist.txt without restarting — the trash/spam/label/
        # unsubscribe operations all check against this, not the file
        # directly, so an in-memory whitelist that predates an edit
        # wouldn't reflect it otherwise.
        self.whitelist_emails, self.whitelist_domains = load_whitelist()

    def reload_whitelist(self):
        self.whitelist_emails, self.whitelist_domains = load_whitelist()

    def is_whitelisted(self, address: str) -> bool:
        return is_whitelisted(address, self.whitelist_emails, self.whitelist_domains)

    def list_messages(
        self,
        user_id: str,
        query: Optional[str] = None,
        only_newer_than: Optional[str] = None,
    ) -> Tuple[List[str], Optional[str]]:
        """
        Lists messages in the user's mailbox with filtering options.

        Args:
            user_id: The user's email address. Special value 'me' indicates the authenticated user.
            query: Optional Gmail search query to filter messages (e.g., "from:example@gmail.com")
            only_newer_than: Optional historyId to fetch only messages newer than this ID

        Returns:
            A tuple containing (list of message IDs, latest historyId)
        """
        try:
            messages = []
            latest_history_id = None

            # Handle incremental updates with history
            # NOTE: currently unused
            if only_newer_than:
                history_response = (
                    self.gmailclient.service.users()
                    .history()
                    .list(userId=user_id, startHistoryId=only_newer_than)
                    .execute()
                )

                if "history" in history_response:
                    pbar = tqdm(desc="Processing history changes", unit="pages")
                    pbar.update(1)

                    for history in history_response["history"]:
                        if "messagesAdded" in history:
                            for msg in history["messagesAdded"]:
                                messages.append(msg["message"]["id"])

                    latest_history_id = history_response.get("historyId")

                    while "nextPageToken" in history_response:
                        page_token = history_response["nextPageToken"]
                        history_response = (
                            self.gmailclient.service.users()
                            .history()
                            .list(
                                userId=user_id,
                                startHistoryId=only_newer_than,
                                pageToken=page_token,
                            )
                            .execute()
                        )

                        if "history" in history_response:
                            for history in history_response["history"]:
                                if "messagesAdded" in history:
                                    for msg in history["messagesAdded"]:
                                        messages.append(msg["message"]["id"])

                        pbar.update(1)
                        latest_history_id = history_response.get(
                            "historyId", latest_history_id
                        )

                    pbar.close()

                messages = list(set(messages))
                self.total_messages = len(messages)
                return messages, latest_history_id

            # Handle regular listing
            params = {}
            if query:
                params["q"] = query

            response = (
                self.gmailclient.service.users()
                .messages()
                .list(userId=user_id, **params)
                .execute()
            )

            if "messages" in response:
                pbar = tqdm(desc="Fetching message pages", unit="pages")
                pbar.update(1)

                messages.extend(message["id"] for message in response["messages"])
                self.total_messages += len(response["messages"])
                pbar.set_postfix({"emails": self.total_messages})

                if "messages" in response and response["messages"]:
                    msg_id = response["messages"][0]["id"]
                    msg = (
                        self.gmailclient.service.users()
                        .messages()
                        .get(userId=user_id, id=msg_id, format="minimal")
                        .execute()
                    )
                    latest_history_id = msg.get("historyId")

                while "nextPageToken" in response:
                    page_token = response["nextPageToken"]
                    params_with_page = params.copy()  # Create a copy to add page token

                    response = (
                        self.gmailclient.service.users()
                        .messages()
                        .list(userId=user_id, pageToken=page_token, **params_with_page)
                        .execute()
                    )

                    if "messages" in response:
                        messages.extend(
                            message["id"] for message in response["messages"]
                        )
                        self.total_messages += len(response["messages"])

                    pbar.update(1)
                    pbar.set_postfix({"emails": self.total_messages})

                pbar.close()

            return messages, latest_history_id

        except Exception as error:
            print(f"An error occurred at list_messages: {error}")
            return [], None

    def get_sender(self, request_id, response, exception):
        """
        Callback function for batch requests that extracts sender information.

        Args:
            request_id: Unique ID of the request
            response: The returned Gmail message object
            exception: Exception object if an error occurred
        """
        if exception:
            print(f"An error occurred during batch processing: {exception}")
            return

        try:
            if response and "payload" in response and "headers" in response["payload"]:
                for header in response["payload"]["headers"]:
                    if header["name"] == "From":
                        self.users.append(header.get("value"))
                        break
            else:
                print(f"Missing expected fields in response for message {request_id}")
        except Exception as error:
            print(f"An error occurred at get_sender for message {request_id}: {error}")

    def _generic_callback(self, request_id, _response, exception, operation: str):
        """
        Generic callback for batch operations.

        Args:
            request_id: ID of the request
            response: Response from the API
            exception: Exception if request failed
            operation: The operation type being performed
        """
        if exception is not None:
            print(f"Error during {operation} for message {request_id}: {exception}")
            return False

        # Update appropriate counter based on operation
        if operation == "trash":
            self.moved_to_trash += 1
        elif operation == "spam":
            self.moved_to_spam += 1
        elif operation == "label":
            self.labels += 1

        return True

    def batch_process(self, items: List[str], operation: str, **kwargs):
        """
        Generic batch processing function for Gmail API operations with exponential backoff.

        Args:
            items: List of IDs or items to process
            operation: Operation type ('trash', 'spam', 'label', 'get')
            **kwargs: Additional arguments needed for specific operations
                - user_id: The user's email address (default 'me')
                - label_id: The label ID (for 'label' operation)

        Returns:
            Number of successfully processed items
        """
        if operation == "trash":
            batch_size = TRASH_BATCH_SIZE
        elif operation == "spam":
            batch_size = SPAM_BATCH_SIZE
        elif operation == "label":
            batch_size = LABEL_BATCH_SIZE
        elif operation == "get":
            batch_size = GET_BATCH_SIZE
        else:
            batch_size = 20

        user_id = kwargs.get("user_id", "me")
        processed_count = 0

        # Define operation-specific configurations
        operations = {
            "trash": {
                "process_batch": lambda batch: self.gmailclient.service.users()
                .messages()
                .batchModify(
                    userId=user_id, body={"addLabelIds": ["TRASH"], "ids": batch}
                )
                .execute(),
                "uses_batch_http": True,
                "desc": "Moving to trash",
                "counter": "moved_to_trash",
            },
            "spam": {
                "process_batch": lambda batch: self.gmailclient.service.users()
                .messages()
                .batchModify(
                    userId=user_id, body={"addLabelIds": ["SPAM"], "ids": batch}
                )
                .execute(),
                "uses_batch_http": True,
                "desc": "Moving to spam",
                "counter": "moved_to_spam",
            },
            "label": {
                "process_batch": lambda batch: self.gmailclient.service.users()
                .messages()
                .batchModify(
                    userId=user_id,
                    body={"addLabelIds": [kwargs.get("label_id")], "ids": batch},
                )
                .execute(),
                "uses_batch_http": True,
                "desc": "Applying label",
                "counter": "labels",
            },
            "get": {
                "create_request": lambda item_id: self.gmailclient.service.users()
                .messages()
                .get(userId=user_id, id=item_id, format="metadata"),
                "callback": self.get_sender,
                "uses_batch_http": False,
                "desc": "Getting messages",
            },
        }

        if operation not in operations:
            raise ValueError(f"Unsupported operation: {operation}")

        op_config = operations[operation]

        # Process in batches with progress tracking
        pbar = tqdm(
            range(0, len(items), batch_size),
            desc=op_config["desc"],
            unit="batch",
        )

        for start_idx in pbar:
            end_idx = min(start_idx + batch_size, len(items))
            batch_items = items[start_idx:end_idx]

            # Implement exponential backoff
            max_retries = 5
            retry_count = 0
            wait_time = 1  # Initial wait time in seconds

            while retry_count <= max_retries:
                try:
                    if op_config.get("uses_batch_http", True):
                        op_config["process_batch"](batch_items)
                        processed_count += len(batch_items)

                        counter_name = op_config.get("counter")
                        if counter_name and hasattr(self, counter_name):
                            setattr(
                                self,
                                counter_name,
                                getattr(self, counter_name) + len(batch_items),
                            )

                        # Success, break out of retry loop
                        break
                    else:
                        # Per-item batch (e.g. "get"): the outer HTTP call can
                        # succeed even when individual sub-requests inside it
                        # are rejected (e.g. 403 rate-limited). Those failures
                        # are reported per-item via the callback, not as an
                        # exception here, so we track them ourselves and retry
                        # only the failed subset with backoff.
                        rate_limited_ids = []
                        user_callback = op_config.get("callback")

                        def _tracking_callback(
                            request_id, response, exception, _failed=rate_limited_ids
                        ):
                            if exception is not None:
                                status = getattr(
                                    getattr(exception, "resp", None), "status", None
                                )
                                if status == 429 or status == 403 or (
                                    status is not None and 500 <= status < 600
                                ):
                                    _failed.append(request_id)
                                    return
                                print(
                                    f"Error during {operation} for message {request_id}: {exception}"
                                )
                                return
                            user_callback(request_id, response, None)

                        batch = self.gmailclient.service.new_batch_http_request(
                            callback=_tracking_callback
                        )

                        for item_id in batch_items:
                            batch.add(
                                op_config["create_request"](item_id), request_id=item_id
                            )

                        batch.execute()

                        succeeded = len(batch_items) - len(rate_limited_ids)
                        processed_count += succeeded

                        if not rate_limited_ids:
                            # Everything in this batch succeeded (or failed
                            # with a non-retryable error) — done.
                            break

                        retry_count += 1
                        if retry_count > max_retries:
                            print(
                                f"Maximum retries exceeded for {len(rate_limited_ids)} "
                                f"rate-limited messages in batch starting at {start_idx}"
                            )
                            break

                        jitter = random.uniform(0.5, 1.5)
                        sleep_time = wait_time * jitter
                        print(
                            f"Rate limit exceeded on {len(rate_limited_ids)} messages, "
                            f"retrying in {sleep_time:.2f} seconds (attempt {retry_count}/{max_retries})"
                        )
                        pbar.set_postfix(
                            {"status": f"Rate limited, retry {retry_count}"}
                        )
                        time.sleep(sleep_time)
                        wait_time *= 2

                        # Only retry the messages that actually failed
                        batch_items = rate_limited_ids

                except HttpError as error:
                    if error.resp.status == 429 or error.resp.status == 403 or (500 <= error.resp.status < 600):
                        retry_count += 1

                        if retry_count > max_retries:
                            print(
                                f"Maximum retries exceeded for batch starting at {start_idx}"
                            )
                            break

                        # Calculate wait time with jitter
                        jitter = random.uniform(0.5, 1.5)
                        sleep_time = wait_time * jitter

                        print(
                            f"Rate limit exceeded, retrying in {sleep_time:.2f} seconds (attempt {retry_count}/{max_retries})"
                        )
                        pbar.set_postfix(
                            {"status": f"Rate limited, retry {retry_count}"}
                        )

                        time.sleep(sleep_time)

                        # Exponential backoff
                        wait_time *= 2
                    else:
                        # If it's not a rate limit or server error, don't retry
                        print(
                            f"Error during {operation} (HTTP {error.resp.status}): {error}"
                        )
                        break
                except Exception as error:
                    print(
                        f"An error occurred during batch processing ({operation}): {error}"
                    )
                    break

        return processed_count

    def batch_get(self, messages: List[str], user_id: str = "me"):
        """
        Process messages in batches to extract sender information.

        Args:
            messages: List of message IDs to process
            user_id: The user's email address (default 'me')
        """
        return self.batch_process(messages, "get", user_id=user_id)

    def get_senders_for_messages(
        self, message_ids: List[str], user_id: str = "me"
    ) -> Dict[str, str]:
        """Fetch just the From header for each message ID, batched. Returns
        {message_id: sender_string}. Deliberately separate from batch_get(),
        which appends to self.users for the "show most common senders"
        feature — this is used for whitelist filtering and shouldn't
        pollute that shared state.
        """
        results: Dict[str, str] = {}

        def _callback(request_id, response, exception):
            if exception is not None:
                return
            headers = (response or {}).get("payload", {}).get("headers", [])
            for header in headers:
                if header["name"] == "From":
                    results[request_id] = header["value"]
                    break

        for start in range(0, len(message_ids), GET_BATCH_SIZE):
            batch_items = message_ids[start : start + GET_BATCH_SIZE]
            batch = self.gmailclient.service.new_batch_http_request(callback=_callback)
            for item_id in batch_items:
                batch.add(
                    self.gmailclient.service.users()
                    .messages()
                    .get(userId=user_id, id=item_id, format="metadata", metadataHeaders=["From"]),
                    request_id=item_id,
                )
            batch.execute()

        return results

    def filter_whitelisted_messages(
        self, message_ids: List[str], user_id: str = "me"
    ) -> Tuple[List[str], int]:
        """Remove any message whose sender is whitelisted. Returns
        (filtered_message_ids, number_removed_because_whitelisted).

        No-ops immediately (zero extra API calls) if the whitelist is
        empty — using this tool without setting one up costs nothing.
        """
        if not message_ids or not (self.whitelist_emails or self.whitelist_domains):
            return message_ids, 0

        senders = self.get_senders_for_messages(message_ids, user_id)

        kept = []
        removed = 0
        for message_id in message_ids:
            sender = senders.get(message_id, "")
            match = re.search(r"<(.+)>", sender)
            address = match.group(1) if match else sender
            if self.is_whitelisted(address):
                removed += 1
            else:
                kept.append(message_id)

        return kept, removed

    def batch_delete(self, messages: List[str], user_id: str = "me"):
        """
        Process messages in batches to move them to trash.

        Whitelisted senders are filtered out first — see
        filter_whitelisted_messages(). This is the enforcement point for
        "never delete this," not just a suggestion respected by whichever
        menu option happened to call it.

        Args:
            messages: List of message IDs to move to trash
            user_id: The user's email address (default 'me')
        """
        messages, skipped = self.filter_whitelisted_messages(messages, user_id)
        if skipped:
            print(f"Skipped {skipped} whitelisted message(s) — not moved to trash.")
        return self.batch_process(messages, "trash", user_id=user_id)

    def batch_spam(self, messages: List[str], user_id: str = "me"):
        """
        Process messages in batches to move them to spam.

        Whitelisted senders are filtered out first — see
        filter_whitelisted_messages().

        Args:
            messages: List of message IDs to move to spam
            user_id: The user's email address (default 'me')
        """
        messages, skipped = self.filter_whitelisted_messages(messages, user_id)
        if skipped:
            print(f"Skipped {skipped} whitelisted message(s) — not moved to spam.")
        return self.batch_process(messages, "spam", user_id=user_id)

    def batch_label(self, messages: List[str], label_id: str, user_id: str = "me"):
        """
        Process messages in batches to apply a label.

        Args:
            messages: List of message IDs to label
            label_id: ID of the label to apply
            user_id: The user's email address (default 'me')

        Returns:
            Number of successfully labeled messages
        """
        return self.batch_process(messages, "label", user_id=user_id, label_id=label_id)

    def list_messages_matching_query(self, user_id: str, query: str = "") -> List[str]:
        """
        List message IDs matching a specific query.

        Args:
            user_id: The user's email address
            query: The search query (e.g., 'from:example@gmail.com')

        Returns:
            A list of message IDs
        """
        try:
            messages = []

            response = (
                self.gmailclient.service.users()
                .messages()
                .list(userId=user_id, q=query)
                .execute()
            )

            if "messages" in response:
                pbar = tqdm(desc=f"Finding emails matching '{query}'", unit="pages")
                pbar.update(1)

                messages.extend(message["id"] for message in response["messages"])
                pbar.set_postfix({"found": len(messages)})

                while "nextPageToken" in response:
                    previous_count = len(messages)
                    page_token = response["nextPageToken"]
                    response = (
                        self.gmailclient.service.users()
                        .messages()
                        .list(userId=user_id, q=query, pageToken=page_token)
                        .execute()
                    )

                    if "messages" in response:
                        messages.extend(
                            message["id"] for message in response["messages"]
                        )

                    pbar.update(1)
                    pbar.set_postfix({"found": len(messages)})

                    if len(messages) == previous_count:
                        break

                pbar.close()

            return messages

        except Exception as error:
            print(f"An error occurred at list_messages_matching_query: {error}")
            return []

    def list_messages_matching_label(self, user_id: str, label_id: str) -> List[str]:
        """
        List message IDs with a specific label.

        Args:
            user_id: The user's email address
            label_id: The label ID

        Returns:
            A list of message IDs
        """
        try:
            messages = []

            response = (
                self.gmailclient.service.users()
                .messages()
                .list(userId=user_id, labelIds=label_id)
                .execute()
            )

            if "messages" in response:
                pbar = tqdm(
                    desc=f"Finding emails with label '{label_id}'", unit="pages"
                )
                pbar.update(1)

                messages.extend(message["id"] for message in response["messages"])
                pbar.set_postfix({"found": len(messages)})

                while "nextPageToken" in response:
                    page_token = response["nextPageToken"]
                    response = (
                        self.gmailclient.service.users()
                        .messages()
                        .list(userId=user_id, labelIds=label_id, pageToken=page_token)
                        .execute()
                    )

                    if "messages" in response:
                        messages.extend(
                            message["id"] for message in response["messages"]
                        )

                    pbar.update(1)
                    pbar.set_postfix({"found": len(messages)})

                pbar.close()

                if messages:
                    print(f"Found {len(messages)} emails with label '{label_id}'")

            return messages

        except Exception as error:
            print(f"An error occurred at list_messages_matching_label: {error}")
            return []

    def list_labels(self, user_id: str) -> List[Dict[str, Any]]:
        """
        List all available labels.

        Args:
            user_id: The user's email address

        Returns:
            A list of label objects
        """
        try:
            response = (
                self.gmailclient.service.users().labels().list(userId=user_id).execute()
            )
            return response.get("labels", [])
        except Exception as error:
            print(f"An error occurred at list_labels: {error}")
            return []

    def create_label(self, user_id: str, label_name: str) -> Dict[str, Any]:
        """
        Create a new label.

        Args:
            user_id: The user's email address
            label_name: The name for the new label

        Returns:
            The created label object
        """
        try:
            response = (
                self.gmailclient.service.users()
                .labels()
                .create(
                    userId=user_id,
                    body={
                        "name": label_name,
                        "messageListVisibility": "SHOW",
                        "labelListVisibility": "LABEL_SHOW",
                        "type": "USER",
                    },
                )
                .execute()
            )
            return response
        except Exception as error:
            print(f"An error occurred at create_label: {error}")
            return {}

    def label_check(self, labels: List[Dict[str, Any]], label_name: str) -> bool:
        """
        Check if a label exists.

        Args:
            labels: List of label objects
            label_name: The label name to check

        Returns:
            True if the label exists, False otherwise
        """
        return any(label["name"] == label_name for label in labels)

    # ========================================================
    # BULK UNSUBSCRIBE
    #
    # Gmail's own "Unsubscribe" button isn't a Gmail feature — it reads the
    # List-Unsubscribe / List-Unsubscribe-Post headers (RFC 2369 / RFC 8058)
    # that a compliant sender includes on bulk mail, and either fires a
    # single background HTTP POST ("one-click unsubscribe") or falls back to
    # a mailto: link or a manual web page. As of 2024 Google *requires*
    # bulk senders to support one-click unsubscribe, so a large share of
    # newsletter/marketing mail can be unsubscribed from programmatically
    # without ever opening a browser.
    # ========================================================

    LIST_UNSUBSCRIBE_HEADERS = ["List-Unsubscribe", "List-Unsubscribe-Post", "From"]

    @staticmethod
    def _parse_list_unsubscribe(header_value: Optional[str]) -> List[str]:
        """Extract the angle-bracket targets from a List-Unsubscribe header.

        e.g. 'List-Unsubscribe: <https://x.com/u?id=1>, <mailto:u@x.com>'
        -> ['https://x.com/u?id=1', 'mailto:u@x.com']
        """
        if not header_value:
            return []
        return re.findall(r"<([^>]+)>", header_value)

    def get_unsubscribe_headers(
        self, message_id: str, user_id: str = "me"
    ) -> Dict[str, str]:
        """Fetch just the List-Unsubscribe headers for one message (cheap —
        metadata format with an explicit header allowlist, not a full fetch).
        """
        msg = (
            self.gmailclient.service.users()
            .messages()
            .get(
                userId=user_id,
                id=message_id,
                format="metadata",
                metadataHeaders=self.LIST_UNSUBSCRIBE_HEADERS,
            )
            .execute()
        )
        headers = msg.get("payload", {}).get("headers", [])
        return {h["name"]: h["value"] for h in headers}

    def _find_representative_headers(
        self, sender_query: str, user_id: str = "me"
    ) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        """Find one message from sender_query and return its
        List-Unsubscribe-relevant headers. Read-only — looks, doesn't act.

        Shared by unsubscribe_sender() (which acts on what it finds) and
        check_unsubscribe_support() (which only reports what it finds, for
        the weekly report) so the lookup logic — and its "search Trash too"
        fix — only exists in one place.

        Returns (headers, error). Exactly one will be None.
        """
        # in:anywhere is required here specifically — unlike the trash/spam/
        # label operations, this one doesn't care whether the message is
        # sitting in Trash. Unsubscribe targets live in the headers, which
        # trashing doesn't touch, and a sender you've already bulk-trashed
        # is exactly the case this needs to keep working for.
        #
        # Only one message is ever needed (unsubscribe targets are per
        # mailing-list, not per message), so this asks the API for exactly
        # one result instead of reusing the general-purpose paginating
        # lookup, which would otherwise enumerate every matching message
        # first — wasted calls and quota for a sender with a large history.
        response = (
            self.gmailclient.service.users()
            .messages()
            .list(userId=user_id, q=f"from:{sender_query} in:anywhere", maxResults=1)
            .execute()
        )
        messages = [m["id"] for m in response.get("messages", [])]
        if not messages:
            return None, "not_found"

        try:
            return self.get_unsubscribe_headers(messages[0], user_id), None
        except HttpError as error:
            return None, str(error)

    def check_unsubscribe_support(
        self, sender_query: str, user_id: str = "me"
    ) -> Dict[str, Any]:
        """Read-only check: does this sender support List-Unsubscribe, and
        of what kind? Never fires a POST, sends mail, or otherwise acts —
        safe to call from the weekly report, which only suggests.
        """
        headers, error = self._find_representative_headers(sender_query, user_id)
        if headers is None:
            return {"supported": False, "kind": error}

        list_unsub = headers.get("List-Unsubscribe")
        if not list_unsub:
            return {"supported": False, "kind": "no_header"}

        list_unsub_post = headers.get("List-Unsubscribe-Post", "")
        https_targets = [
            t for t in self._parse_list_unsubscribe(list_unsub) if t.lower().startswith("http")
        ]
        one_click = "one-click" in list_unsub_post.lower() and bool(https_targets)

        return {"supported": True, "kind": "one_click" if one_click else "manual"}

    def unsubscribe_sender(
        self, sender_query: str, user_id: str = "me"
    ) -> Dict[str, Any]:
        """Attempt to unsubscribe from one sender automatically.

        Looks at a single representative message (unsubscribe targets are
        per mailing-list, not per message, so one is enough), and:
          - if the sender supports RFC 8058 one-click unsubscribe, fires the
            POST directly and reports success/failure
          - if only a mailto: or a plain web link is available, reports that
            back rather than guessing — sending an email or loading an
            unknown page on your behalf are different, riskier actions than
            reading your own mailbox, so this stops short and tells you
            instead of acting on it silently.

        Returns a dict with at least a "status" key:
          not_found | no_header | unsubscribed_one_click | http_error |
          request_failed | manual_link_only | mailto_only | unknown_format
          | whitelisted
        """
        # Checked before any lookup at all — a whitelisted entry should
        # never even trigger the API calls to look for an unsubscribe
        # target, not just be denied afterward.
        if self.is_whitelisted(sender_query):
            return {"status": "whitelisted", "sender": sender_query, "from": sender_query}

        headers, error = self._find_representative_headers(sender_query, user_id)
        if headers is None:
            status = "fetch_failed" if error and error != "not_found" else "not_found"
            result = {"status": status, "sender": sender_query}
            if status == "fetch_failed":
                result["error"] = error
            return result

        from_addr = headers.get("From", sender_query)

        # Second check on the resolved address: sender_query might have
        # been a loose match (part of an OR chain, a display name, etc.)
        # that happens to resolve to a whitelisted address even though the
        # query string itself didn't obviously look like one.
        if self.is_whitelisted(from_addr):
            return {"status": "whitelisted", "sender": sender_query, "from": from_addr}

        list_unsub = headers.get("List-Unsubscribe")
        list_unsub_post = headers.get("List-Unsubscribe-Post", "")

        if not list_unsub:
            return {"status": "no_header", "sender": sender_query, "from": from_addr}

        targets = self._parse_list_unsubscribe(list_unsub)
        https_targets = [t for t in targets if t.lower().startswith("http")]
        mailto_targets = [t for t in targets if t.lower().startswith("mailto:")]

        one_click = "one-click" in list_unsub_post.lower()

        if https_targets and one_click:
            url = https_targets[0]
            try:
                resp = requests.post(
                    url,
                    data={"List-Unsubscribe": "One-Click"},
                    timeout=10,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; GmailBulkUnsubscribe/1.0)"
                    },
                )
                if 200 <= resp.status_code < 300:
                    return {
                        "status": "unsubscribed_one_click",
                        "sender": sender_query,
                        "from": from_addr,
                        "url": url,
                    }
                return {
                    "status": "http_error",
                    "sender": sender_query,
                    "from": from_addr,
                    "url": url,
                    "code": resp.status_code,
                }
            except requests.RequestException as error:
                return {
                    "status": "request_failed",
                    "sender": sender_query,
                    "from": from_addr,
                    "url": url,
                    "error": str(error),
                }

        if https_targets:
            # No List-Unsubscribe-Post header, so no RFC 8058 guarantee that
            # a POST (or even a GET) is safe/idempotent here. Report it
            # rather than guess — this is the case that still needs a
            # browser.
            return {
                "status": "manual_link_only",
                "sender": sender_query,
                "from": from_addr,
                "url": https_targets[0],
            }

        if mailto_targets:
            # Actually sending mail on your behalf is a different, riskier
            # action than anything else this tool does. Report the address
            # instead of sending anything automatically.
            return {
                "status": "mailto_only",
                "sender": sender_query,
                "from": from_addr,
                "target": mailto_targets[0],
            }

        return {"status": "unknown_format", "sender": sender_query, "from": from_addr}

    def bulk_unsubscribe(self, senders: List[str], user_id: str = "me") -> Dict[str, List[Dict[str, Any]]]:
        """Run unsubscribe_sender() over a list of senders and bucket the
        results by outcome, so the caller can report a clean summary.
        """
        results: Dict[str, List[Dict[str, Any]]] = {}
        for sender in tqdm(senders, desc="Unsubscribing", unit="sender"):
            outcome = self.unsubscribe_sender(sender, user_id)
            results.setdefault(outcome["status"], []).append(outcome)
            # Be a polite citizen of other people's infrastructure — this
            # isn't Gmail's API anymore, it's a POST to a third-party
            # mailing-list host, one per sender, not one per message, so a
            # small delay costs nothing and avoids hammering anyone.
            time.sleep(0.5)
        return results

    # ========================================================
    # EMAIL SEND — used only by the weekly clutter report to send the
    # summary to yourself. Not exposed as a general "send to anyone" menu
    # option — this tool reads and organizes your mailbox, it doesn't
    # contact other people on your behalf.
    # ========================================================

    def get_own_email_address(self, user_id: str = "me") -> str:
        """The authenticated account's own address, so the report always
        emails the right inbox without hardcoding it.
        """
        profile = self.gmailclient.service.users().getProfile(userId=user_id).execute()
        return profile["emailAddress"]

    def send_email(
        self, to: str, subject: str, body: str, user_id: str = "me"
    ) -> Dict[str, Any]:
        """Send a plain-text email via the Gmail API."""
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        return (
            self.gmailclient.service.users()
            .messages()
            .send(userId=user_id, body={"raw": raw})
            .execute()
        )

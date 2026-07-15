#!/usr/bin/python3
"""Unit tests for meeting class and attendee bucketing helpers."""

import unittest

from gcal_classify import (
    attendee_buckets,
    build_color_map,
    classify_event,
    parse_class_pairs,
)


class ParseClassPairsTest(unittest.TestCase):
    def test_parses_pairs(self):
        self.assertEqual(
            parse_class_pairs(["triage=TRIAGE:", "incident=INC:"]),
            [("triage", "TRIAGE:"), ("incident", "INC:")],
        )

    def test_rejects_invalid(self):
        with self.assertRaises(ValueError):
            parse_class_pairs(["noequals"])


class ClassifyEventTest(unittest.TestCase):
    def test_prefix_first_match_wins(self):
        prefixes = [("triage", "TRIAGE:"), ("other", "TRIAGE:X")]
        event = {"summary": "TRIAGE: board"}
        self.assertEqual(classify_event(event, prefixes, {}), "triage")

    def test_prefix_case_insensitive(self):
        prefixes = [("triage", "TRIAGE:")]
        event = {"summary": "triage: daily"}
        self.assertEqual(classify_event(event, prefixes, {}), "triage")

    def test_colour_when_no_prefix(self):
        event = {"summary": "Planning", "colorId": "11"}
        color_map = build_color_map(["incident=11"])
        self.assertEqual(classify_event(event, [], color_map), "incident")

    def test_prefix_beats_colour(self):
        event = {"summary": "TRIAGE: stuff", "colorId": "11"}
        prefixes = [("triage", "TRIAGE:")]
        color_map = build_color_map(["incident=11"])
        self.assertEqual(classify_event(event, prefixes, color_map), "triage")

    def test_unclassified(self):
        event = {"summary": "Random meeting"}
        self.assertEqual(classify_event(event, [], {}), "unclassified")


class AttendeeBucketsTest(unittest.TestCase):
    def test_optionality_and_response(self):
        event = {
            "attendees": [
                {"email": "a@example.com", "responseStatus": "accepted"},
                {
                    "email": "b@example.com",
                    "optional": True,
                    "responseStatus": "declined",
                },
                {"email": "c@example.com", "responseStatus": "tentative"},
                {"email": "d@example.com"},
            ]
        }
        self.assertEqual(
            list(attendee_buckets(event)),
            [
                ("mandatory", "accepted"),
                ("optional", "declined"),
                ("mandatory", "tentative"),
                ("mandatory", "needsAction"),
            ],
        )

    def test_skips_self_and_resource(self):
        event = {
            "attendees": [
                {"email": "me@example.com", "self": True, "responseStatus": "accepted"},
                {
                    "email": "room@example.com",
                    "resource": True,
                    "responseStatus": "accepted",
                },
                {"email": "peer@example.com", "responseStatus": "accepted"},
            ]
        }
        self.assertEqual(
            list(attendee_buckets(event)),
            [("mandatory", "accepted")],
        )

    def test_unknown_response_becomes_needs_action(self):
        event = {
            "attendees": [
                {"email": "a@example.com", "responseStatus": "weird"},
            ]
        }
        self.assertEqual(
            list(attendee_buckets(event)),
            [("mandatory", "needsAction")],
        )


if __name__ == "__main__":
    unittest.main()

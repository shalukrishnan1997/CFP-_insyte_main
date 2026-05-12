"""Management command to populate service categories and items for invoice settings."""

from decimal import Decimal
from typing import Any, cast

from django.core.management.base import BaseCommand

from invoices.models import ServiceCategory, ServiceItem


class Command(BaseCommand):
    help = "Populate service categories and items for invoice line item selection"

    def handle(self, *args: Any, **options: Any) -> None:
        self.stdout.write("Populating service categories and items...")

        # Define all service data
        service_data = [
            {
                "category": "Set Ups",
                "items": [
                    {
                        "desc": "Payment Gateway integration",
                        "price": "400.00",
                        "unit": "each",
                        "notes": "ie: SagePay, Barclay Card, WorldPay, Global Payments, Stripe (one off cost)",
                    },
                    {
                        "desc": "Other Gateways",
                        "price": "120.00",
                        "unit": "per hour",
                        "notes": "",
                    },
                    {
                        "desc": "Initial Set up",
                        "price": "600.00",
                        "unit": "each",
                        "notes": "Includes 1st campaign set up",
                    },
                    {
                        "desc": "Campaign Set ups",
                        "price": "395.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Emergency appeal campaign set ups",
                        "price": "660.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Raffle campaign set up",
                        "price": "500.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Fulfilment file set up",
                        "price": "250.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Any additional IT development",
                        "price": "120.00",
                        "unit": "per hour",
                        "notes": "",
                    },
                    {
                        "desc": "Direct integration into Raisers Edge, Sales Force, ThankQ",
                        "price": "120.00",
                        "unit": "per hour",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Letter Set Up",
                "items": [
                    {
                        "desc": "Letter set up",
                        "price": "28.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Letter text amends",
                        "price": "28.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Letter proof",
                        "price": "28.00",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Account Management",
                "items": [
                    {
                        "desc": "Account Management",
                        "price": "25.00",
                        "unit": "per hour",
                        "notes": "Per month",
                    },
                ],
            },
            {
                "category": "Exports, Reports and Storage",
                "items": [
                    {
                        "desc": "Import File",
                        "price": "15.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Export File",
                        "price": "15.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Data storage/compliance/secure back up",
                        "price": "5.00",
                        "unit": "per GB per month",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Head Office Post",
                "items": [
                    {
                        "desc": "Receive mail, vet, batch & date stamp",
                        "price": "30.00",
                        "unit": "per hour",
                        "notes": "",
                    },
                    {
                        "desc": "Sortation, validation and scanning",
                        "price": "0.50",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Data Capture Response Handling",
                "items": [
                    {
                        "desc": "Open, batch, sort",
                        "price": "0.18",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Scan (simplex)",
                        "price": "0.10",
                        "unit": "per page",
                        "notes": "",
                    },
                    {
                        "desc": "Data capture - warm (Search by URN, data capture payment type, value, gift aid, processing of cheques, cards, banking schedules & paying in slips)",
                        "price": "0.28",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Data capture - cold (Search by URN, data capture payment type, value, gift aid, processing of cheques, cards, banking schedules & paying in slips)",
                        "price": "0.42",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Data capture debit or credit card & redaction",
                        "price": "0.48",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Data capture Direct Debit",
                        "price": "0.65",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Data capture additional fields (tel, email, DOB)",
                        "price": "0.14",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Data capture tick box & verification",
                        "price": "0.04",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Change of address",
                        "price": "0.21",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "CAF voucher report - preparation and send to agency",
                        "price": "25.00",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Banking",
                "items": [
                    {
                        "desc": "Banking Cheque, Cash, PO",
                        "price": "0.10",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Print A4",
                "items": [
                    {
                        "desc": "Print on letterhead A4 B+W simplex",
                        "price": "0.18",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Print on letterhead A4 B+W duplex",
                        "price": "0.24",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Print on letterhead A4 colour simplex",
                        "price": "0.31",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Print on letterhead colour duplex",
                        "price": "0.37",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Laser letter A4+1/3 B+W simplex",
                        "price": "0.35",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Laser letter A4+1/3 B+W duplex",
                        "price": "0.42",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Print A3",
                "items": [
                    {
                        "desc": "Print on letterhead A3 colour simplex",
                        "price": "0.69",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Print on letterhead A3 colour duplex",
                        "price": "0.92",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Print on Demand",
                "items": [
                    {
                        "desc": "Digital print A4 colour simplex",
                        "price": "0.28",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Digital print A4 colour duplex",
                        "price": "0.37",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Digital print A4 simplex B+W",
                        "price": "0.18",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Digital print A4 duplex B+W",
                        "price": "0.24",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Laser letter A4 + 1/3 B+W simplex",
                        "price": "0.33",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Laser letter A4 +1/3 B+ W duplex",
                        "price": "0.38",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Laser print A3 colour simplex",
                        "price": "0.65",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Laser print A3 colour duplex",
                        "price": "0.86",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Folding",
                "items": [
                    {
                        "desc": "Fold letter once",
                        "price": "0.10",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Fold letter twice",
                        "price": "0.12",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Fold non paper items (tote bag, T-shirt etc)",
                        "price": "0.07",
                        "unit": "per fold",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Enclosing",
                "items": [
                    {
                        "desc": "Enclose additional items into specified order",
                        "price": "0.09",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Enclose first insert",
                        "price": "0.15",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Enclose each additional insert",
                        "price": "0.09",
                        "unit": "each",
                        "notes": "",
                    },
                    {"desc": "Matching", "price": "0.04", "unit": "each", "notes": ""},
                    {
                        "desc": "Enclose magazine (up to 100 pages)",
                        "price": "0.12",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Pick and enclose single non paper item (pin badge, balloon etc)",
                        "price": "0.31",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Enclose into specific location",
                        "price": "0.05",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Enclose each insert",
                        "price": "0.09",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Whitemail",
                "items": [
                    {
                        "desc": "Read free comments, collate, manually write information & make ready for posting",
                        "price": "0.58",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Other Response Handling - Manual",
                "items": [
                    {
                        "desc": "Data capture payment queries (if required)",
                        "price": "0.25",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Separate, batch and index query letter",
                        "price": "0.35",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Scan issue letter",
                        "price": "0.10",
                        "unit": "per page",
                        "notes": "",
                    },
                    {
                        "desc": "Data capture name on cheque",
                        "price": "0.14",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Data capture name of bank",
                        "price": "0.14",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Email address amendments",
                        "price": "0.21",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Process anonymous donations - Search for anon, data capture details, check additional correspondence for verification",
                        "price": "0.61",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "High Gift Value - prepare of email and file audit (includes 1 attachment)",
                        "price": "1.42",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Manually enter segment code",
                        "price": "0.08",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Non financial response",
                        "price": "0.21",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Manually count each piece of mail",
                        "price": "0.06",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Additional sortation (raffle etc)",
                        "price": "0.05",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Manual opening of post (if unable to be machine opened)",
                        "price": "0.28",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Sortation of returns",
                        "price": "0.07",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Secure shredding",
                        "price": "25.00",
                        "unit": "per bag",
                        "notes": "",
                    },
                    {
                        "desc": "Supply, print and affix address label",
                        "price": "0.11",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Affix sticker",
                        "price": "0.10",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Supply, make up box",
                        "price": "1.23",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Storage",
                "items": [
                    {
                        "desc": "Pallet storage",
                        "price": "5.50",
                        "unit": "per pallet per week",
                        "notes": "",
                    },
                    {
                        "desc": "Box storage",
                        "price": "2.20",
                        "unit": "per box per week",
                        "notes": "",
                    },
                    {
                        "desc": "Receive and book in goods at HQ",
                        "price": "25.00",
                        "unit": "per hour",
                        "notes": "",
                    },
                    {
                        "desc": "Receive and book in goods at HQ (pallets)",
                        "price": "5.50",
                        "unit": "per pallet",
                        "notes": "",
                    },
                    {
                        "desc": "Receive and book in goods at HQ (boxes)",
                        "price": "2.20",
                        "unit": "per box per week",
                        "notes": "",
                    },
                    {
                        "desc": "Full manual stock check",
                        "price": "28.00",
                        "unit": "per hour",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Destruction",
                "items": [
                    {
                        "desc": "Secure pallet destruction",
                        "price": "175.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Secure shredding",
                        "price": "20.00",
                        "unit": "per bag",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Fulfilment Services",
                "items": [
                    {
                        "desc": "Pick and pack first item (small up to A3 size)",
                        "price": "0.85",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Pick and pack each additional same item",
                        "price": "0.65",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Pick and pack first item (large item over A3 size)",
                        "price": "1.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Pick and pack each additional same item (large)",
                        "price": "0.65",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Pin Badges",
                "items": [
                    {
                        "desc": "Enclose pin badge",
                        "price": "0.25",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Enclose pin badge into specific place",
                        "price": "0.32",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Collection Tin/bucket",
                "items": [
                    {
                        "desc": "Pick and enclose first collection tin/bucket",
                        "price": "1.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Each additional collection tin/bucket",
                        "price": "0.75",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Outer materials",
                "items": [
                    {
                        "desc": "D1 Jiffy bag",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "E2 Jiffy bag",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "H5 Jiffy bag",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "K7 Jiffy bag",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "A4 box",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "A3 box",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "C5 window plain white outer envelope",
                        "price": "0.04",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "C4 window plain white outer envelope",
                        "price": "0.06",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Telephones",
                "items": [
                    {
                        "desc": "Number set up",
                        "price": "275.00",
                        "unit": "each",
                        "notes": "one off cost - 12 mth contract",
                    },
                    {
                        "desc": "Setup recorded messages",
                        "price": "55.00",
                        "unit": "each",
                        "notes": "one off cost",
                    },
                    {
                        "desc": "Re-record message",
                        "price": "25.00",
                        "unit": "per message",
                        "notes": "",
                    },
                    {
                        "desc": "Monthly line rental charges",
                        "price": "39.00",
                        "unit": "per month",
                        "notes": "12 mth contract",
                    },
                ],
            },
            {
                "category": "Supporter Care (Monday-Friday 9am-5pm)",
                "items": [
                    {
                        "desc": "Inbound - up to 2 mins",
                        "price": "1.65",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Inbound - each additional min thereafter",
                        "price": "0.65",
                        "unit": "per minute",
                        "notes": "",
                    },
                    {
                        "desc": "Inbound - operator only",
                        "price": "25.00",
                        "unit": "per hour",
                        "notes": "min 2 people",
                    },
                    {
                        "desc": "Log query onto system",
                        "price": "1.75",
                        "unit": "per minute",
                        "notes": "",
                    },
                    {
                        "desc": "Outbound - up to 2 mins",
                        "price": "2.15",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Outbound - each min thereafter",
                        "price": "0.40",
                        "unit": "per minute",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Postage",
                "items": [
                    {
                        "desc": "UK 2nd class letter",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "UK 1st class letter - recorded delivery",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "UK 1st class large letter - recorded delivery",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "Set up freepost address",
                        "price": "120.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Freepost & outbound postage",
                        "price": "0.00",
                        "unit": "at cost",
                        "notes": "At cost",
                    },
                ],
            },
            {
                "category": "Courier",
                "items": [
                    {
                        "desc": "Courier (dependant on service, destination, weight and dimensions)",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                ],
            },
            {
                "category": "Raffle Processing",
                "items": [
                    {
                        "desc": "Open mail, vet & batch",
                        "price": "0.26",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Warm without donation",
                        "price": "0.65",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Warm with donation",
                        "price": "0.68",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Cold without donation",
                        "price": "0.76",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Cold with donation",
                        "price": "0.79",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Warm debit card without donation",
                        "price": "0.67",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Warm debit card with donation",
                        "price": "0.70",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Cold debit card without donation",
                        "price": "0.72",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Cold debit card with donation",
                        "price": "0.77",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Card verification",
                        "price": "0.06",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Debit/credit card redaction",
                        "price": "0.14",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Raffle Other",
                "items": [
                    {
                        "desc": "Additional sortation",
                        "price": "0.05",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Sold ticket data capture",
                        "price": "0.25",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Tear off and separate sold/unsold tickets",
                        "price": "0.05",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Handwritten stubs",
                        "price": "0.16",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Separate bought raffle tickets for box draw",
                        "price": "0.06",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Supervise raffle draw",
                        "price": "40.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Host draw",
                        "price": "180.00",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Raffle Player",
                "items": [
                    {
                        "desc": "Raffle Player 1",
                        "price": "1500.00",
                        "unit": "each",
                        "notes": "Cost covers initial set up. Standard template and font",
                    },
                    {
                        "desc": "Raffle Player 2",
                        "price": "4750.00",
                        "unit": "each",
                        "notes": "Cost covers initial build. Standard template and adaptable font",
                    },
                    {
                        "desc": "Raffle Player 3 (Bespoke)",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Bespoke - dependant on client scope of works/requirement",
                    },
                ],
            },
            {
                "category": "Raffle Refresh Fees",
                "items": [
                    {
                        "desc": "Raffle Player 1: For each campaign iteration thereafter, eg changes to images & copy",
                        "price": "750.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Raffle Player 2: for each iteration thereafter, dependant on scope & additional requests",
                        "price": "850.00",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Raffle Additional Development",
                "items": [
                    {
                        "desc": "Development time to cover additional scope or functionality",
                        "price": "120.00",
                        "unit": "per hour",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Raffle Transaction Fees",
                "items": [
                    {
                        "desc": "£1 - £7,500",
                        "price": "0.00",
                        "unit": "20% of income",
                        "notes": "20% all income up to £7,500",
                    },
                    {
                        "desc": "£7,500 - £12,500",
                        "price": "0.00",
                        "unit": "15% of income",
                        "notes": "15% the income between these two values",
                    },
                    {
                        "desc": "£12,501 +",
                        "price": "0.00",
                        "unit": "10% of income",
                        "notes": "10% The income above this value",
                    },
                    {
                        "desc": "Transaction fees for donation related income",
                        "price": "0.00",
                        "unit": "0%",
                        "notes": "We do not charge a processing fee for donations made online",
                    },
                ],
            },
            {
                "category": "Raffle Draw & Reporting",
                "items": [
                    {
                        "desc": "Draw Fee",
                        "price": "250.00",
                        "unit": "per draw",
                        "notes": "",
                    },
                    {
                        "desc": "Reporting Fee",
                        "price": "120.00",
                        "unit": "per hour",
                        "notes": "Includes weekly email of results and end to end campaign data dump. Covers changes to standard scope/requirements of reporting",
                    },
                ],
            },
            {
                "category": "Raffle Account Management",
                "items": [
                    {
                        "desc": "Account management",
                        "price": "500.00",
                        "unit": "per month",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Raffle Strategy",
                "items": [
                    {
                        "desc": "Strategy - dependant on requirement and budget",
                        "price": "1000.00",
                        "unit": "per month",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Raffle Print",
                "items": [
                    {
                        "desc": "Standard pack",
                        "price": "0.35",
                        "unit": "per pack",
                        "notes": "dependant on format - Variation of book sizes (1-3 books)",
                    },
                    {
                        "desc": "First insert",
                        "price": "0.15",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Each additional insert",
                        "price": "0.09",
                        "unit": "each",
                        "notes": "",
                    },
                    {"desc": "Fold once", "price": "0.10", "unit": "each", "notes": ""},
                    {
                        "desc": "Fold twice",
                        "price": "0.12",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Raffle Creative",
                "items": [
                    {
                        "desc": "Standard refresh",
                        "price": "5000.00",
                        "unit": "each",
                        "notes": "Dependent on specifications",
                    },
                    {
                        "desc": "New pack with concepts",
                        "price": "8000.00",
                        "unit": "each",
                        "notes": "Dependent on specifications",
                    },
                ],
            },
            {
                "category": "Data Processing",
                "items": [
                    {
                        "desc": "To process mail ready",
                        "price": "21.00",
                        "unit": "per thousand",
                        "notes": "",
                    },
                    {
                        "desc": "Suppressions",
                        "price": "0.40",
                        "unit": "per hit",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Cold Data",
                "items": [
                    {
                        "desc": "Cold data processing - dependant on specification",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                ],
            },
            {
                "category": "Subscription Raffle",
                "items": [
                    {
                        "desc": "Web development fee",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "Software",
                        "price": "500.00",
                        "unit": "per month",
                        "notes": "",
                    },
                    {
                        "desc": "Processing",
                        "price": "500.00",
                        "unit": "per month",
                        "notes": "",
                    },
                    {
                        "desc": "Charge on income amount",
                        "price": "0.00",
                        "unit": "5%",
                        "notes": "5% of income",
                    },
                    {
                        "desc": "Administration",
                        "price": "150.00",
                        "unit": "per week",
                        "notes": "",
                    },
                    {
                        "desc": "File charges",
                        "price": "8.50",
                        "unit": "each",
                        "notes": "assumed min of 2",
                    },
                    {
                        "desc": "Letter costs (admin letters etc)",
                        "price": "1.40",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Postage",
                        "price": "0.00",
                        "unit": "at cost",
                        "notes": "At cost",
                    },
                ],
            },
            {
                "category": "60/40 Raffle",
                "items": [
                    {
                        "desc": "Administration (standard for 1000 players +)",
                        "price": "0.00",
                        "unit": "16.67% per draw",
                        "notes": "16.67% of the lottery proceeds processed that week (per draw)",
                    },
                    {
                        "desc": "Prizes (standard for 1000 players +)",
                        "price": "0.00",
                        "unit": "20% per draw",
                        "notes": "20% of the lottery proceeds processed that week (per draw)",
                    },
                    {
                        "desc": "Administration (<1000 players)",
                        "price": "0.00",
                        "unit": "30%",
                        "notes": "for lotteries forecast to be <1000 players in the first 12 month we change commercials to 60/40 to 50/50 until 1000 players is reached",
                    },
                    {
                        "desc": "Prizes (<1000 players)",
                        "price": "0.00",
                        "unit": "20%",
                        "notes": "for lotteries forecast to be <1000 players",
                    },
                ],
            },
            {
                "category": "Website",
                "items": [
                    {
                        "desc": "Standard build",
                        "price": "2750.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Website hosting",
                        "price": "55.00",
                        "unit": "per month",
                        "notes": "",
                    },
                    {
                        "desc": "Bespoke build",
                        "price": "0.00",
                        "unit": "POA per month",
                        "notes": "Dependent on requirements",
                    },
                ],
            },
            {
                "category": "Keep The Change (KTC)",
                "items": [
                    {
                        "desc": "Base set up",
                        "price": "1750.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Keep The Change web development & exports",
                        "price": "950.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Donation Processing",
                        "price": "0.00",
                        "unit": "5%",
                        "notes": "5% of donation amount",
                    },
                ],
            },
            {
                "category": "SUN",
                "items": [
                    {"desc": "Setup", "price": "650.00", "unit": "each", "notes": ""},
                    {
                        "desc": "Print of standard materials Letterheads, flyers, BRE's",
                        "price": "750.00",
                        "unit": "each",
                        "notes": "",
                    },
                    {
                        "desc": "Design of letterhead, flyers, BRE's",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "Full creative identity",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "Bulk transfer of existing players Internal admin charge",
                        "price": "0.00",
                        "unit": "POA",
                        "notes": "Price on application",
                    },
                    {
                        "desc": "Per letter hit fee",
                        "price": "1.39",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
            {
                "category": "Direct Mail Pack",
                "items": [
                    {
                        "desc": "Standard Pack",
                        "price": "0.35",
                        "unit": "each",
                        "notes": "",
                    },
                ],
            },
        ]

        created_categories = 0
        created_items = 0

        for idx, service_group in enumerate(service_data, start=1):
            category, created = ServiceCategory.objects.get_or_create(
                name=service_group["category"],
                defaults={"is_active": True, "order": idx},
            )
            if created:
                created_categories += 1
                self.stdout.write(
                    self.style.SUCCESS(f"Created category: {category.name}")
                )

            for item_idx, item_data in enumerate(
                cast(list[dict[str, str]], service_group["items"]), start=1
            ):
                _service_item, created = ServiceItem.objects.get_or_create(
                    category=category,
                    description=item_data["desc"],
                    defaults={
                        "unit_price": Decimal(item_data["price"]),
                        "pricing_unit": item_data["unit"],
                        "notes": item_data["notes"],
                        "is_active": True,
                        "is_default": False,
                        "order": item_idx,
                    },
                )
                if created:
                    created_items += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nCompleted! Created {created_categories} categories and {created_items} service items."
            )
        )

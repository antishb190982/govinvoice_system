import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from ocr.services import process_queue


class Command(BaseCommand):
    help = "Analyse queued invoice documents (OCR). Use --loop in a worker container."

    def add_arguments(self, parser):
        parser.add_argument("--loop", type=int, default=0, help="Poll every N seconds")

    def handle(self, *args, loop=0, **options):
        while True:
            close_old_connections()
            done = process_queue(limit=10)
            if done or not loop:
                self.stdout.write(f"analysed={done}")
            if not loop:
                break
            if not done:
                time.sleep(loop)

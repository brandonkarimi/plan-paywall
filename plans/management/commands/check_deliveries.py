"""Delivery guarantee job. Schedule every 15 minutes (Render cron job): python manage.py check_deliveries"""
from django.core.management.base import BaseCommand

from plans.services.delivery import check_deliveries


class Command(BaseCommand):
    help = "Retry delivery for paid projects not yet delivered; auto-refund those past the guarantee window."

    def handle(self, **_):
        self.stdout.write(str(check_deliveries()))

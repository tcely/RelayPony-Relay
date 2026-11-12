from django.db import models


class SignalBlob(models.Model):
    id = models.BigAutoField(primary_key=True)
    recipient = models.CharField(max_length=128)
    payload = models.TextField()
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "relaypony_signal"
        indexes = [
            models.Index(fields=["recipient", "expires_at"]),
        ]


class RelayDatum(models.Model):
    id = models.BigAutoField(primary_key=True)
    recipient = models.CharField(max_length=128)
    session = models.CharField(max_length=64)
    payload = models.TextField()
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "relaypony_relaydata"
        indexes = [
            models.Index(fields=["recipient", "session", "expires_at"]),
        ]


class Nameplate(models.Model):
    number = models.PositiveSmallIntegerField(unique=True)  # 1..9999 fits
    token_hash = models.CharField(max_length=64)            # sha256 hex of the token
    expires_at = models.DateTimeField(db_index=True)


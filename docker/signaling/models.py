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


class RateLimit(models.Model):
    id = models.BigAutoField(primary_key=True)
    bucket = models.CharField(max_length=64)   # sha256(salt | ip), hex
    op = models.CharField(max_length=16)
    at = models.DateTimeField()

    class Meta:
        db_table = 'relaypony_ratelimit'


class Nameplate(models.Model):
    nameplate = models.PositiveIntegerField(primary_key=True)
    token = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = 'relaypony_nameplate'


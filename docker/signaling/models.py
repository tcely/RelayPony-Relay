from django.db import models


class SignalBlob(models.Model):
    id = models.BigAutoField(primary_key=True)
    recipient = models.CharField(max_length=128, db_index=True)
    payload = models.TextField()
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "relaypony_signal"


class RelayDatum(models.Model):
    id = models.BigAutoField(primary_key=True)
    recipient = models.CharField(max_length=128, db_index=True)
    session = models.CharField(max_length=64, db_index=True)
    payload = models.TextField()
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "relaypony_relaydata"


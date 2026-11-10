from django.db import models


class SignalBlob(models.Model):
    id = models.BigAutoField(primary_key=True)
    recipient = models.CharField(max_length=128)
    payload = models.TextField()
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "relaypony_signal"
        managed = False


class RelayDatum(models.Model):
    id = models.BigAutoField(primary_key=True)
    recipient = models.CharField(max_length=128)
    session = models.CharField(max_length=64)
    payload = models.TextField()
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "relaypony_relaydata"
        managed = False


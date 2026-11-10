from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="SignalBlob",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("recipient", models.CharField(db_index=True, max_length=128)),
                ("payload", models.TextField()),
                ("expires_at", models.DateTimeField(db_index=True)),
            ],
            options={"db_table": "relaypony_signal"},
        ),
        migrations.CreateModel(
            name="RelayDatum",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("recipient", models.CharField(db_index=True, max_length=128)),
                ("session", models.CharField(db_index=True, max_length=64)),
                ("payload", models.TextField()),
                ("expires_at", models.DateTimeField(db_index=True)),
            ],
            options={"db_table": "relaypony_relaydata"},
        ),
    ]


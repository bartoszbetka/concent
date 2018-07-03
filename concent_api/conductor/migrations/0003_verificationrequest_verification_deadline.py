# -*- coding: utf-8 -*-
# Generated by Django 1.11.9 on 2018-06-29 10:56
from __future__ import unicode_literals

import datetime
from django.db import migrations, models
from django.utils.timezone import utc


class Migration(migrations.Migration):

    dependencies = [
        ('conductor', '0002_auto_20180627_1335'),
    ]

    operations = [
        migrations.AddField(
            model_name='verificationrequest',
            name='verification_deadline',
            field=models.DateTimeField(default=datetime.datetime(2018, 7, 1, 0, 0, 0, 0, tzinfo=utc)),
            preserve_default=False,
        ),
    ]

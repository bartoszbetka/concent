# -*- coding: utf-8 -*-
# Generated by Django 1.11.9 on 2018-06-05 16:24
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import utils.fields


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Client',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_key', utils.fields.Base64Field(db_column='public_key', max_length=64, unique=True)),
            ],
        ),
        migrations.CreateModel(
            name='PaymentInfo',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('payment_ts', models.DateTimeField()),
                ('task_owner_key', models.BinaryField()),
                ('provider_eth_account', models.CharField(max_length=42)),
                ('amount_paid', models.IntegerField()),
                ('recipient_type', models.CharField(choices=[('Provider', 'provider'), ('Requestor', 'requestor')], max_length=32)),
                ('amount_pending', models.IntegerField()),
            ],
        ),
        migrations.CreateModel(
            name='PendingResponse',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('response_type', models.CharField(choices=[('ForceReportComputedTask', 'ForceReportComputedTask'), ('ForceReportComputedTaskResponse', 'ForceReportComputedTaskResponse'), ('VerdictReportComputedTask', 'VerdictReportComputedTask'), ('ForceGetTaskResultRejected', 'ForceGetTaskResultRejected'), ('ForceGetTaskResultFailed', 'ForceGetTaskResultFailed'), ('ForceGetTaskResultUpload', 'ForceGetTaskResultUpload'), ('ForceGetTaskResultDownload', 'ForceGetTaskResultDownload'), ('ForceSubtaskResults', 'ForceSubtaskResults'), ('SubtaskResultsSettled', 'SubtaskResultsSettled'), ('ForceSubtaskResultsResponse', 'ForceSubtaskResultsResponse'), ('SubtaskResultsRejected', 'SubtaskResultsRejected'), ('ForcePaymentCommitted', 'ForcePaymentCommitted')], max_length=32)),
                ('queue', models.CharField(choices=[('Receive', 'receive'), ('ReceiveOutOfBand', 'receive_out_of_band')], max_length=32)),
                ('delivered', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('client', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='core.Client')),
            ],
        ),
        migrations.CreateModel(
            name='StoredMessage',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('type', models.PositiveSmallIntegerField()),
                ('timestamp', models.DateTimeField()),
                ('data', models.BinaryField()),
                ('task_id', models.CharField(blank=True, max_length=128, null=True)),
                ('subtask_id', models.CharField(blank=True, max_length=128, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='Subtask',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('task_id', models.CharField(max_length=128)),
                ('subtask_id', models.CharField(db_index=True, max_length=128, unique=True)),
                ('state', models.CharField(choices=[('FORCING_REPORT', 'forcing_report'), ('REPORTED', 'reported'), ('FORCING_RESULT_TRANSFER', 'forcing_result_transfer'), ('RESULT_UPLOADED', 'result_uploaded'), ('FORCING_ACCEPTANCE', 'forcing_acceptance'), ('REJECTED', 'rejected'), ('VERIFICATION_FILE_TRANSFER', 'verification_file_transfer'), ('ADDITIONAL_VERIFICATION', 'additional_verification'), ('ACCEPTED', 'accepted'), ('FAILED', 'failed')], max_length=32)),
                ('next_deadline', models.DateTimeField(blank=True, null=True)),
                ('ack_report_computed_task', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='subtasks_for_ack_report_computed_task', to='core.StoredMessage')),
                ('provider', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='subtasks_as_provider', to='core.Client')),
                ('reject_report_computed_task', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='subtasks_for_reject_report_computed_task', to='core.StoredMessage')),
                ('report_computed_task', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='subtasks_for_report_computed_task', to='core.StoredMessage')),
                ('requestor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='subtasks_as_requestor', to='core.Client')),
                ('subtask_results_accepted', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='subtasks_for_subtask_results_accepted', to='core.StoredMessage')),
                ('subtask_results_rejected', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='subtasks_for_subtask_results_rejected', to='core.StoredMessage')),
                ('task_to_compute', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='subtasks_for_task_to_compute', to='core.StoredMessage')),
            ],
        ),
        migrations.AddField(
            model_name='pendingresponse',
            name='subtask',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='core.Subtask'),
        ),
        migrations.AddField(
            model_name='paymentinfo',
            name='pending_response',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payments', to='core.PendingResponse'),
        ),
        migrations.AlterUniqueTogether(
            name='subtask',
            unique_together=set([('requestor', 'subtask_id'), ('requestor', 'task_id')]),
        ),
    ]

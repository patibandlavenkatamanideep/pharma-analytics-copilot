# PostgreSQL, private, encrypted, TLS only. RDS generates the administrator's
# password and keeps it in Secrets Manager (manage_master_user_password), so
# it never passes through Terraform state, a variable or a shell.

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg${var.db_engine_version}"
  family = "postgres${var.db_engine_version}"

  # Refuse unencrypted connections; the application verifies the server's
  # certificate (PGSSLMODE=verify-full with the image's RDS CA bundle).
  # Declared as AWS stores it (pending-reboot; in force from creation, and 1
  # is PostgreSQL 16's default), or every plan shows a change that is not one.
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "password_encryption"
    value = "scram-sha-256"
  }

  # bootstrap_db.py sets role passwords with ALTER ROLE ... PASSWORD; a failed
  # statement is otherwise logged with its text, and these logs go to
  # CloudWatch. No statement text is logged, failed or not.
  parameter {
    name  = "log_min_error_statement"
    value = "panic"
  }

  parameter {
    name  = "log_statement"
    value = "none"
  }
}

# RDS creates this group on first export, with no expiry; created here first
# so it has the same retention as every other log.
resource "aws_cloudwatch_log_group" "db" {
  name              = "/aws/rds/instance/${var.name}/postgresql"
  retention_in_days = var.log_retention_days
}

resource "aws_db_instance" "this" {
  identifier                  = var.name
  engine                      = "postgres"
  engine_version              = var.db_engine_version
  instance_class              = var.db_instance_class
  allocated_storage           = var.db_allocated_storage_gb
  max_allocated_storage       = var.db_allocated_storage_gb * 3
  storage_type                = "gp3"
  storage_encrypted           = true
  username                    = "pacadmin"
  manage_master_user_password = true
  db_subnet_group_name        = aws_db_subnet_group.this.name
  vpc_security_group_ids      = [aws_security_group.db.id]
  parameter_group_name        = aws_db_parameter_group.this.name
  ca_cert_identifier          = "rds-ca-rsa2048-g1"
  publicly_accessible         = false
  multi_az                    = var.db_multi_az
  backup_retention_period     = var.db_backup_retention_days
  copy_tags_to_snapshot       = true
  deletion_protection         = var.deletion_protection
  skip_final_snapshot         = false
  final_snapshot_identifier   = "${var.name}-final-${var.final_snapshot_label}"
  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false
  apply_immediately           = false

  iam_database_authentication_enabled = false
  performance_insights_enabled        = false
  enabled_cloudwatch_logs_exports     = ["postgresql"]

  depends_on = [aws_cloudwatch_log_group.db]
}

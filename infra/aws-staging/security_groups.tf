# Who may talk to whom. Terraform removes AWS's default allow-all egress from
# each group it creates, so every path below is the only one.

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "The load balancer: HTTPS (and the redirect from HTTP) from the allowed ranges only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each          = toset(var.allowed_cidrs)
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from an allowed range"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each          = toset(var.allowed_cidrs)
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from an allowed range, redirected to HTTPS"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

# Only when the owner opens the sign-in page to everyone (var.public_sign_in):
# the load balancer's two listeners, nothing else.
resource "aws_vpc_security_group_ingress_rule" "alb_https_public" {
  count             = var.public_sign_in ? 1 : 0
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from anywhere: the sign-in page (owner decision, public_sign_in)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "alb_http_public" {
  count             = var.public_sign_in ? 1 : 0
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from anywhere, redirected to HTTPS (owner decision, public_sign_in)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_app" {
  security_group_id            = aws_security_group.alb.id
  description                  = "To the application tasks"
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "Serving tasks: inbound from the load balancer only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "app_from_alb" {
  security_group_id            = aws_security_group.app.id
  description                  = "From the load balancer"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_security_group" "jobs" {
  name        = "${var.name}-jobs"
  description = "One-shot tasks (bootstrap, migrations, loads): no inbound at all"
  vpc_id      = aws_vpc.this.id
}

# Tasks reach AWS APIs (ECR, Secrets Manager, CloudWatch Logs, Bedrock,
# Prometheus remote write) over HTTPS, and the database over PostgreSQL.
resource "aws_vpc_security_group_egress_rule" "https" {
  for_each          = { app = aws_security_group.app.id, jobs = aws_security_group.jobs.id }
  security_group_id = each.value
  description       = "HTTPS to AWS service endpoints"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "postgres" {
  for_each                     = { app = aws_security_group.app.id, jobs = aws_security_group.jobs.id }
  security_group_id            = each.value
  description                  = "To the database"
  referenced_security_group_id = aws_security_group.db.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "PostgreSQL: from the serving and one-shot tasks only; no outbound"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "db" {
  for_each                     = { app = aws_security_group.app.id, jobs = aws_security_group.jobs.id }
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from ${each.key} tasks"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

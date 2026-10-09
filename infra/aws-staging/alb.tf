# HTTPS from the allowed ranges only. The load balancer terminates TLS with an
# ACM certificate for var.hostname; HTTP redirects to HTTPS.

resource "aws_lb" "this" {
  name                       = var.name
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = aws_subnet.public[*].id
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  # The load balancer appends the address it received the connection from to
  # any X-Forwarded-For the client sent; uvicorn, trusting only the VPC range
  # (FORWARDED_ALLOW_IPS), takes the rightmost untrusted entry -- that one --
  # so a client cannot choose the address its failed sign-ins count against.
  xff_header_processing_mode = "append"
  # Longer than the 60 s request deadline.
  idle_timeout = 75
}

resource "aws_lb_target_group" "app" {
  name        = "${var.name}-app"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.this.id
  # In-flight answers finish within the 65 s uvicorn grace period.
  deregistration_delay = 70

  # Readiness, not liveness: false until a dataset is published, so a task
  # with no data never takes traffic.
  health_check {
    path                = "/ready"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_acm_certificate" "this" {
  domain_name       = var.hostname
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "validation" {
  for_each = var.route53_zone_id == null ? {} : {
    for o in aws_acm_certificate.this.domain_validation_options : o.domain_name => o
  }
  zone_id = var.route53_zone_id
  name    = each.value.resource_record_name
  type    = each.value.resource_record_type
  records = [each.value.resource_record_value]
  ttl     = 300
}

# Waits until the certificate is issued. Without a zone, create the CNAME
# from the acm_validation_records output (README.md, "Certificate").
resource "aws_acm_certificate_validation" "this" {
  certificate_arn         = aws_acm_certificate.this.arn
  validation_record_fqdns = var.route53_zone_id == null ? null : [for r in aws_route53_record.validation : r.fqdn]
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.this.certificate_arn

  # Browser protections the application does not send, added to every
  # response: HTTPS only for a year (this host; HTTP only redirects), no
  # content-type guessing, never framed (the sign-in page is public).
  routing_http_response_strict_transport_security_header_value = "max-age=31536000"
  routing_http_response_x_content_type_options_header_value    = "nosniff"
  routing_http_response_x_frame_options_header_value           = "DENY"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_route53_record" "app" {
  count   = var.route53_zone_id == null ? 0 : 1
  zone_id = var.route53_zone_id
  name    = var.hostname
  type    = "A"
  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = false
  }
}

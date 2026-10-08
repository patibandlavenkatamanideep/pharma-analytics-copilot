output "url" {
  description = "The staging URL, once DNS points at the load balancer and the certificate is issued."
  value       = "https://${var.hostname}"
}

output "load_balancer_dns_name" {
  description = "Point var.hostname at this (alias or CNAME) when no Route 53 zone is managed here."
  value       = aws_lb.this.dns_name
}

output "acm_validation_records" {
  description = "The DNS record that proves control of var.hostname, if created by hand."
  value       = [for o in aws_acm_certificate.this.domain_validation_options : { name = o.resource_record_name, type = o.resource_record_type, value = o.resource_record_value }]
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "image" {
  description = "What the tasks run: the repository and the digest, never a tag. Null until image_digest is set."
  value       = local.image
}

output "cluster" {
  value = aws_ecs_cluster.this.name
}

output "service" {
  value = one(aws_ecs_service.app[*].name)
}

output "one_shot_task_families" {
  value = { for k, t in aws_ecs_task_definition.job : k => t.family }
}

output "run_task_network" {
  description = "Network settings for aws ecs run-task (README.md, \"Order of operations\")."
  value = {
    subnets          = local.task_subnets
    security_groups  = [aws_security_group.jobs.id]
    assign_public_ip = local.nat ? "DISABLED" : "ENABLED"
  }
}

output "db_endpoint" {
  value = aws_db_instance.this.address
}

output "secret_arns" {
  description = "Containers only; their values are set by seed-secrets.sh or the owner."
  value = merge({ for r, s in aws_secretsmanager_secret.db_role : "db_${r}" => s.arn }, {
    test_users  = aws_secretsmanager_secret.test_users.arn
    oidc_client = aws_secretsmanager_secret.oidc_client.arn
    rds_admin   = aws_db_instance.this.master_user_secret[0].secret_arn
  })
}

output "github_publish_role_arn" {
  description = "For .github/workflows/publish-staging.yml: assumed only by a job in the GitHub environment var.github_environment; pushes images, nothing else."
  value       = aws_iam_role.publish.arn
}

output "prometheus_workspace" {
  value = one(aws_prometheus_workspace.this[*].id)
}

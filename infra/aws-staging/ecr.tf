# The release image, by digest. Tags cannot be overwritten, so a tag names
# one image forever; deployment still uses the digest (var.image_digest).

resource "aws_ecr_repository" "app" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Routine expiry keeps the last 20 images, but never one tagged keep-*: the
# image deployed now and the one designated for recovery carry that tag
# (README.md, "Deploy"). A running task does not prove a replacement task can
# still pull its image. ECR does not let a lower-priority rule expire an image
# that a higher-priority rule selects.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Never expire an image marked for deployment or recovery (keep-*)"
        selection    = { tagStatus = "tagged", tagPrefixList = ["keep-"], countType = "imageCountMoreThan", countNumber = 9999 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Otherwise keep the last 20 images"
        selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
        action       = { type = "expire" }
      },
    ]
  })
}

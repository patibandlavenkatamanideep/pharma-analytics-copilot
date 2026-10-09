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

# Routine expiry keeps the last 20 images, except those tagged keep-*: the
# image deployed now and the one designated for recovery carry that tag
# (README.md, "Deploy"). A running task does not prove a replacement task can
# still pull its image. ECR has no "keep" action, so the first rule selects
# keep-* images and expires them only beyond 9,999 of them -- retained up to
# that limit, not forever; the README keeps the tag on two images. ECR does
# not let a lower-priority rule expire an image a higher-priority rule
# selects; a lifecycle policy preview confirms it once the repository exists.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Retain images marked for deployment or recovery (keep-*), up to 9,999 of them"
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

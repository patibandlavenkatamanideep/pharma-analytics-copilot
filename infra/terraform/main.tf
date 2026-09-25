# Pharma Analytics Copilot -- single-instance deployment.
#
# Reproduces by code what infra/README.md section B describes by hand: one EC2
# instance running the app, PostgreSQL and Caddy under Docker Compose, with a
# real Let's Encrypt certificate obtained through sslip.io so no domain has to
# be bought.
#
#   terraform init
#   terraform apply -var="key_name=pac-deploy" -var="my_ip=$(curl -s ifconfig.me)/32"
#
# What this deliberately does NOT create: an RDS instance. The database runs in
# a container on the same host, which is the trade-off documented in
# infra/README.md -- appropriate for an assessment, not for production. The
# rds.tf file next to this one has that variant, commented, for when it is.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# ---------------------------------------------------------------------------
# Networking: default VPC is enough for one host. A production deployment
# would put the database in a private subnet; here it is in a container that
# publishes no port at all, which achieves the same isolation more cheaply.
# ---------------------------------------------------------------------------

data "aws_vpc" "default" {
  default = true
}

resource "aws_security_group" "app" {
  name_prefix = "${var.name}-"
  description = "Pharma Analytics Copilot: public HTTPS, restricted SSH"
  vpc_id      = data.aws_vpc.default.id

  # 80 is required for the Let's Encrypt HTTP-01 challenge, not for serving.
  # Caddy redirects it to 443.
  ingress {
    description = "ACME HTTP-01 challenge and redirect to HTTPS"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # SSH is restricted to one address rather than left open. Administration
  # can also go through SSM Session Manager, which needs no inbound rule at
  # all -- see the instance profile below.
  ingress {
    description = "SSH from the operator only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip]
  }

  egress {
    description = "Outbound: Bedrock, ACME, package registries"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = var.name }
}

# ---------------------------------------------------------------------------
# IAM: the instance calls Bedrock with a role, never with a key on disk.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name_prefix        = "${var.name}-"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "bedrock" {
  statement {
    sid     = "InvokeTheOnePlannerModel"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    # Scoped to the planner model rather than "*": this instance has exactly
    # one reason to call Bedrock.
    resources = [
      "arn:aws:bedrock:*::foundation-model/${var.bedrock_model_id}",
      "arn:aws:bedrock:*:*:inference-profile/${var.bedrock_inference_profile}",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock" {
  name_prefix = "${var.name}-bedrock-"
  role        = aws_iam_role.instance.id
  policy      = data.aws_iam_policy_document.bedrock.json
}

# Session Manager, so the host can be administered without opening SSH.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "instance" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.instance.name
}

# ---------------------------------------------------------------------------
# The instance
# ---------------------------------------------------------------------------

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-*-arm64"]
  }
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  key_name               = var.key_name
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  # The dataset is 2,000,000 sales rows plus indexes, and the generator needs
  # room to write CSVs before they are loaded.
  root_block_device {
    volume_size           = var.disk_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  # IMDSv2 only: a stolen SSRF cannot read instance credentials from v1.
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  user_data = templatefile("${path.module}/user_data.sh", {
    repo_url = var.repo_url
  })

  tags = { Name = var.name }
}

resource "aws_eip" "app" {
  instance = aws_instance.app.id
  domain   = "vpc"
  tags     = { Name = var.name }
}

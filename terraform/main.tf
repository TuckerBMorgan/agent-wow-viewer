# This child module is applied from agent-wow, using its existing state/provider.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "aws_region" { type = string }
variable "partition" { type = string }
variable "vpc_id" { type = string }
variable "availability_zone" { type = string }
variable "route_table_id" { type = string }
variable "security_group_id" { type = string }
variable "instance_role_name" { type = string }
variable "instance_assume_role_policy" { type = string }
variable "instance_profile_arn" { type = string }
variable "job_role_id" { type = string }
variable "results_bucket_arn" { type = string }
variable "ecr_repository_arn" { type = string }
variable "ecr_repository_url" { type = string }
variable "agent_launch_template_id" { type = string }
variable "agent_launch_template_version" { type = number }

data "aws_caller_identity" "viewer" {}

# Keep optional viewer/build capacity out of the busy Batch availability zone.
resource "aws_subnet" "viewer" {
  vpc_id                  = var.vpc_id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true
}
resource "aws_route_table_association" "viewer" {
  subnet_id      = aws_subnet.viewer.id
  route_table_id = var.route_table_id
}
# The viewer is started on demand by viewer.py; apply never starts a Windows VM.
resource "aws_iam_role_policy_attachment" "instance_ssm" {
  role       = var.instance_role_name
  policy_arn = "arn:${var.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "viewer_game" {
  role = var.job_role_id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "s3:PutObject", Resource = "${var.results_bucket_arn}/viewer/*/account.json" },
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = var.ecr_repository_arn
      }
    ]
  })
}

resource "aws_iam_role" "viewer" {
  name_prefix        = "agent-wow-viewer-"
  assume_role_policy = var.instance_assume_role_policy
}
resource "aws_iam_role_policy_attachment" "viewer_ssm" {
  role       = aws_iam_role.viewer.name
  policy_arn = "arn:${var.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "viewer" {
  role = aws_iam_role.viewer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${var.results_bucket_arn}/viewer/*" },
      { Effect = "Allow", Action = "s3:GetObject", Resource = ["arn:aws:s3:::dcv-license.${var.aws_region}/*"] },
      { Effect = "Allow", Action = "batch:DescribeJobs", Resource = "*" },
      {
        Effect    = "Allow", Action = "ssm:StartSession"
        Resource  = ["arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.viewer.account_id}:instance/*"]
        Condition = { StringEquals = { "ssm:resourceTag/ViewerEnabled" = "true" } }
      },
      { Effect = "Allow", Action = "ssm:StartSession", Resource = aws_ssm_document.viewer_ports.arn },
      { Effect = "Allow", Action = ["ssm:TerminateSession", "ssm:ResumeSession"], Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.viewer.account_id}:session/*" }
    ]
  })
}
resource "aws_iam_instance_profile" "viewer" {
  name_prefix = "agent-wow-viewer-"
  role        = aws_iam_role.viewer.name
}
resource "aws_ssm_document" "viewer_ports" {
  name          = "agent-wow-game-port"
  document_type = "Session"
  content = jsonencode({
    schemaVersion = "1.0"
    sessionType   = "Port"
    parameters = {
      portNumber      = { type = "String", allowedValues = ["3724", "8085"] }
      localPortNumber = { type = "String", allowedValues = ["3724", "8085"] }
    }
    properties = { portNumber = "{{portNumber}}", localPortNumber = "{{localPortNumber}}", type = "LocalPortForwarding" }
  })
}
resource "aws_launch_template" "viewer" {
  name_prefix                          = "agent-wow-viewer-"
  instance_type                        = "m6a.2xlarge"
  instance_initiated_shutdown_behavior = "terminate"
  vpc_security_group_ids               = [var.security_group_id]
  iam_instance_profile { arn = aws_iam_instance_profile.viewer.arn }
  metadata_options {
    http_tokens = "required"
  }
  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = 100
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "agent-wow-viewer", Project = "agent-wow" }
  }
}

resource "aws_iam_role" "world_build" {
  name_prefix        = "agent-wow-world-build-"
  assume_role_policy = var.instance_assume_role_policy
}
resource "aws_iam_role_policy_attachment" "world_build_ssm" {
  role       = aws_iam_role.world_build.name
  policy_arn = "arn:${var.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "world_build" {
  role = aws_iam_role.world_build.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${var.results_bucket_arn}/viewer/build/*" },
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["ecr:DescribeImages", "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"]
        Resource = var.ecr_repository_arn
      }
    ]
  })
}
resource "aws_iam_instance_profile" "world_build" {
  name_prefix = "agent-wow-world-build-"
  role        = aws_iam_role.world_build.name
}
data "aws_ssm_parameter" "builder_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}
resource "aws_launch_template" "world_build" {
  name_prefix                          = "agent-wow-world-build-"
  image_id                             = data.aws_ssm_parameter.builder_ami.value
  instance_type                        = "m6a.2xlarge"
  instance_initiated_shutdown_behavior = "terminate"
  vpc_security_group_ids               = [var.security_group_id]
  iam_instance_profile { arn = aws_iam_instance_profile.world_build.arn }
  metadata_options { http_tokens = "required" }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 100
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "agent-wow-world-build", Project = "agent-wow" }
  }
}

output "viewer_launch_template" { value = aws_launch_template.viewer.id }
output "viewer_subnet" { value = aws_subnet.viewer.id }
output "world_build_subnet" { value = aws_subnet.viewer.id }
output "world_build_template" { value = aws_launch_template.world_build.id }
output "world_watch_image" { value = "${var.ecr_repository_url}:world-watch-9fc017d9-cb9fa4a8" }

# Separate test capacity leaves existing jobs on their original hosts/images.
resource "aws_batch_compute_environment" "viewer" {
  name_prefix = "agent-wow-viewer-"
  type        = "MANAGED"
  state       = "ENABLED"
  compute_resources {
    type                = "EC2"
    allocation_strategy = "BEST_FIT_PROGRESSIVE"
    min_vcpus           = 0
    max_vcpus           = 8
    instance_type       = ["m6a.2xlarge"]
    instance_role       = var.instance_profile_arn
    subnets             = [aws_subnet.viewer.id]
    security_group_ids  = [var.security_group_id]
    tags                = { Project = "agent-wow", ViewerEnabled = "true" }
    ec2_configuration { image_type = "ECS_AL2023" }
    launch_template {
      launch_template_id = var.agent_launch_template_id
      version            = tostring(var.agent_launch_template_version)
    }
  }
  lifecycle { ignore_changes = [compute_resources[0].desired_vcpus] }
  depends_on = [aws_iam_role_policy_attachment.instance_ssm, aws_route_table_association.viewer]
}
resource "aws_batch_job_queue" "viewer" {
  name     = "agent-wow-viewer"
  state    = "ENABLED"
  priority = 100
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.viewer.arn
  }
}
output "viewer_job_queue" { value = aws_batch_job_queue.viewer.name }

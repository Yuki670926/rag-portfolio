data "archive_file" "ingest" {
  type        = "zip"
  source_dir  = "${path.root}/../lambda/ingest"
  output_path = "${path.root}/../lambda/ingest.zip"
}

data "archive_file" "query" {
  type        = "zip"
  source_dir  = "${path.root}/../lambda/query"
  output_path = "${path.root}/../lambda/query.zip"
}

# IAMロール
resource "aws_iam_role" "lambda" {
  name = "${var.project_name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.project_name}-lambda-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = "${var.documents_bucket_arn}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "aoss:APIAccessAll"
        ]
        Resource = "*"
      }
    ]
  })
}

# ingest Lambda
resource "aws_lambda_function" "ingest" {
  filename         = data.archive_file.ingest.output_path
  function_name    = "${var.project_name}-ingest"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 300
  source_code_hash = data.archive_file.ingest.output_base64sha256

  environment {
    variables = {
      OPENSEARCH_ENDPOINT = var.opensearch_endpoint
    }
  }

  tags = {
    Name = "${var.project_name}-ingest"
  }
}

# query Lambda
resource "aws_lambda_function" "query" {
  filename         = data.archive_file.query.output_path
  function_name    = "${var.project_name}-query"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 60
  source_code_hash = data.archive_file.query.output_base64sha256

  environment {
    variables = {
      OPENSEARCH_ENDPOINT = var.opensearch_endpoint
    }
  }

  tags = {
    Name = "${var.project_name}-query"
  }
}

# S3からLambdaを呼び出す権限
resource "aws_lambda_permission" "s3_ingest" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = var.documents_bucket_arn
}
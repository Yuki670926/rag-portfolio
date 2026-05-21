data "archive_file" "presigned_url" {
  type        = "zip"
  source_dir  = "${path.root}/../lambda/presigned_url"
  output_path = "${path.root}/../lambda/presigned_url.zip"
}

resource "aws_lambda_function" "presigned_url" {
  filename         = data.archive_file.presigned_url.output_path
  function_name    = "${var.project_name}-presigned-url"
  role             = var.lambda_role_arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 30
  source_code_hash = data.archive_file.presigned_url.output_base64sha256

  environment {
    variables = {
      DOCUMENTS_BUCKET = var.documents_bucket_name
    }
  }

  tags = {
    Name = "${var.project_name}-presigned-url"
  }
}

resource "aws_api_gateway_resource" "upload" {
  rest_api_id = var.rest_api_id
  parent_id   = var.root_resource_id
  path_part   = "upload"
}

resource "aws_api_gateway_method" "upload_post" {
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.upload.id
  http_method   = "POST"
  authorization = "COGNITO_USER_POOLS"
  authorizer_id = var.authorizer_id
}

resource "aws_api_gateway_integration" "upload_lambda" {
  rest_api_id             = var.rest_api_id
  resource_id             = aws_api_gateway_resource.upload.id
  http_method             = aws_api_gateway_method.upload_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.presigned_url.invoke_arn
}

resource "aws_api_gateway_method" "upload_options" {
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.upload.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "upload_options" {
  rest_api_id = var.rest_api_id
  resource_id = aws_api_gateway_resource.upload.id
  http_method = aws_api_gateway_method.upload_options.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "upload_options" {
  rest_api_id = var.rest_api_id
  resource_id = aws_api_gateway_resource.upload.id
  http_method = aws_api_gateway_method.upload_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

resource "aws_api_gateway_integration_response" "upload_options" {
  rest_api_id = var.rest_api_id
  resource_id = aws_api_gateway_resource.upload.id
  http_method = aws_api_gateway_method.upload_options.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,Authorization'"
    "method.response.header.Access-Control-Allow-Methods" = "'POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
  }
  depends_on = [aws_api_gateway_integration.upload_options]
}

resource "aws_lambda_permission" "api_gateway_presigned" {
  statement_id  = "AllowAPIGatewayInvokePresigned"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.presigned_url.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${var.execution_arn}/*/*"
}

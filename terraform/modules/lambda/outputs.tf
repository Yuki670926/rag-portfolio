output "ingest_lambda_arn" {
  value = aws_lambda_function.ingest.arn
}

output "query_lambda_arn" {
  value = aws_lambda_function.query.arn
}

output "query_lambda_invoke_arn" {
  value = aws_lambda_function.query.invoke_arn
}

output "lambda_role_arn" {
  value = aws_iam_role.lambda.arn
}

# 命名規則

## 基本フォーマット
{prefix}-{env}-{service}-{resource}-{purpose}

## 各要素の定義

| 要素 | 説明 | 値 |
|------|------|-----|
| prefix | プロジェクト略称 | rp |
| env | 環境 | dev / stag / prod |
| service | サービス名 | lambda / s3 / cognito / apigw / aoss / cf / vpc |
| resource | リソース種別 | func / bucket / pool / role / policy / api / etc |
| purpose | 用途 | ingest / query / documents / frontend / main / etc |

## 具体例

| 現在 | 新命名規則 |
|------|-----------|
| rag-portfolio-ingest | rp-{env}-lambda-func-ingest |
| rag-portfolio-query | rp-{env}-lambda-func-query |
| rag-portfolio-lambda-role | rp-{env}-iam-role-lambda |
| rag-portfolio-documents-{id} | rp-{env}-s3-bucket-documents |
| rag-portfolio-frontend-{id} | rp-{env}-s3-bucket-frontend |
| rag-portfolio-user-pool | rp-{env}-cognito-pool-main |
| rag-portfolio-api | rp-{env}-apigw-api-main |
| rag-portfolio-collection | rp-{env}-aoss-collection-main |
| rag-portfolio-vpc | rp-{env}-vpc-main |

## タグ設計

全リソースに以下のタグを付与する。

```hcl
default_tags {
  tags = {
    Project     = "rag-portfolio"
    Environment = var.environment
    ManagedBy   = "terraform"
    Owner       = "Yuki670926"
  }
}
```

## 適用方針

- 3環境分割（dev/stag/prod）実装時に新命名規則を適用
- 既存リソースはそのまま残し新環境から新命名規則を使用
- S3バケット名変更時はデータ移行不要（テスト用データのみ）
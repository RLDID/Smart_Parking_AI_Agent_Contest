import { Badge, Button, Card, Facts, Head, Loading, Notice, RouteLink } from '../components';
import { text } from './api';
import { useResource } from './context';

export function DriverCommandDetail({ id }: { id: string }) {
  const resource = useResource(`/api/v1/commands/${encodeURIComponent(id)}`);
  const item = resource.value;
  const labels: Record<string, string> = { pending: '처리 대기', running: '처리 중', succeeded: '완료', failed: '실패', held: '보류', partial: '일부 완료', unknown: '미확인', cancelled: '취소' };
  return <><RouteLink to="/driver/vehicle" className="back">내 차량·요청 이력으로</RouteLink><Head title="내 요청"/>
    {resource.loading ? <Loading/> : resource.error ? <Notice title={resource.error}/> : item && <Card title="접수 내용" action={<Badge>{labels[text(item.aggregate_status)] || text(item.aggregate_status)}</Badge>}>
      <p style={{ whiteSpace: 'pre-wrap' }}>{text(item.request_text)}</p><Facts items={[
        ['요청', id], ['회차', text(item.run_id)], ['접수 시각', text(item.created_at)], ['갱신 시각', text(item.updated_at)], ['버전', text(item.resource_version)],
      ]}/><p className="form-note section-gap">신고·요청의 접수와 차량 이동·사건 해결은 별도 상태입니다.</p>
    </Card>}<Button onClick={resource.reload}>다시 조회</Button>
  </>;
}
